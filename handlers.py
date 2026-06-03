from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, ChatJoinRequest, Message, User as TgUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config import Settings
from keyboards import approval_keyboard, join_keyboard, plans_keyboard
from models import InviteLink, PaymentRequest, User
from services import as_utc, create_invite_for_user, new_expiry, utcnow

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def make_router(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="premium_bot")

    async def upsert_user(session: AsyncSession, telegram_user: TgUser) -> User:
        user = await session.get(User, telegram_user.id)
        if user is None:
            user = User(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                full_name=telegram_user.full_name,
            )
            session.add(user)
        else:
            user.username = telegram_user.username
            user.full_name = telegram_user.full_name
        return user

    def is_admin(user_id: int) -> bool:
        return user_id in settings.admin_ids

    def expiry_text(value) -> str:
        value = as_utc(value)
        return value.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST") if value else "—"

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not message.from_user:
            return
        async with sessions() as session:
            await upsert_user(session, message.from_user)
            await session.commit()
        await message.answer(Welcome
            "Welcome! Choose your premium plan, complete the payment, and upload your payment screenshot. After admin approval, you will get access to the premium channel and group:",
            reply_markup=plans_keyboard(settings.plans),
        )

    @router.callback_query(F.data.startswith("plan:"))
    async def choose_plan(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.data:
            return
        code = callback.data.split(":", maxsplit=1)[1]
        plan = settings.plans.get(code)
        if plan is None:
            await callback.answer("Invalid plan.", show_alert=True)
            return
        async with sessions() as session:
            user = await upsert_user(session, callback.from_user)
            user.selected_plan = plan.code
            user.payment_status = "awaiting_screenshot"
            await session.commit()
        text = (
            f"Plan: {plan.name}\n"
            f"Amount: {plan.amount}\n\n"
            "Thank You! Once your payment is completed, kindly upload the payment screenshot here for verification."
            "It will be sent to the admin for verification Up to 12hour"
        )
        if callback.message:
            await callback.message.answer(text)
        await callback.answer("Plan selected")

    @router.message(Command("renew"))
    async def renew(message: Message) -> None:
        await message.answer("Renewal ke liye plan choose karein:", reply_markup=plans_keyboard(settings.plans))

    @router.message(Command("myplan"))
    async def my_plan(message: Message) -> None:
        if not message.from_user:
            return
        async with sessions() as session:
            user = await session.get(User, message.from_user.id)
        if user is None or user.premium_status != "active":
            await message.answer("Aapka koi active premium plan nahi hai. /start se plan choose karein.")
            return
        plan = settings.plans.get(user.active_plan or "")
        name = plan.name if plan else (user.active_plan or "Premium")
        await message.answer(
            f"Current plan: {name}\n"
            f"Status: Active\n"
            f"Expiry: {expiry_text(user.premium_expires_at)}\n"
            f"Access: {user.channel_access_status}"
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(settings.support_text)

    @router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
    async def receive_screenshot(message: Message, bot: Bot) -> None:
        if not message.from_user:
            return
        async with sessions() as session:
            user = await upsert_user(session, message.from_user)
            if not user.selected_plan or user.selected_plan not in settings.plans:
                await session.commit()
                await message.answer("Pehle /start se premium plan choose karein, phir screenshot bhejein.")
                return
            existing = await session.scalar(
                select(PaymentRequest).where(
                    PaymentRequest.user_id == user.telegram_id,
                    PaymentRequest.status == "pending",
                )
            )
            if existing:
                await message.answer("Aapka screenshot already approval ke liye pending hai.")
                return
            plan = settings.plans[user.selected_plan]
            if message.photo:
                file_id = message.photo[-1].file_id
                kind = "photo"
            elif message.document:
                file_id = message.document.file_id
                kind = "document"
            else:
                return
            request = PaymentRequest(
                user_id=user.telegram_id,
                plan_code=plan.code,
                plan_name=plan.name,
                amount=plan.amount,
                screenshot_file_id=file_id,
                screenshot_kind=kind,
            )
            session.add(request)
            user.payment_status = "pending"
            await session.flush()
            request_id = request.id
            await session.commit()

        handle = f"@{message.from_user.username}" if message.from_user.username else "No username"
        caption = (
            "New payment screenshot\n\n"
            f"Request ID: {request_id}\n"
            f"User: {message.from_user.full_name} ({handle})\n"
            f"User ID: {message.from_user.id}\n"
            f"Plan: {plan.name}\n"
            f"Amount: {plan.amount}"
        )
        sent_to_admin = False
        for admin_id in settings.admin_ids:
            try:
                if kind == "photo":
                    await bot.send_photo(admin_id, file_id, caption=caption, reply_markup=approval_keyboard(request_id))
                else:
                    await bot.send_document(admin_id, file_id, caption=caption, reply_markup=approval_keyboard(request_id))
                sent_to_admin = True
            except TelegramAPIError as exc:
                logger.error("Unable to send payment request %s to admin %s: %s", request_id, admin_id, exc)
        if sent_to_admin:
            await message.answer("Screenshot received. Admin approval ka wait karein.")
        else:
            await message.answer("Screenshot saved, lekin admin notification send nahi hui. Please contact admin @SRKSupports.")

    @router.callback_query(F.data.startswith("pay:"))
    async def payment_decision(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.from_user or not callback.data:
            return
        if not is_admin(callback.from_user.id):
            await callback.answer("Admin only.", show_alert=True)
            return
        try:
            _, action, request_text = callback.data.split(":")
            request_id = int(request_text)
        except (ValueError, TypeError):
            await callback.answer("Invalid action.", show_alert=True)
            return

        invite: InviteLink | None = None
        approved_user_id: int | None = None
        expires_at = None
        async with sessions() as session:
            async with session.begin():
                payment = await session.scalar(
                    select(PaymentRequest).where(PaymentRequest.id == request_id).with_for_update()
                )
                if payment is None:
                    await callback.answer("Payment request not found.", show_alert=True)
                    return
                if payment.status != "pending":
                    await callback.answer(f"Already {payment.status}.", show_alert=True)
                    return
                user = await session.get(User, payment.user_id)
                if user is None:
                    await callback.answer("User not found.", show_alert=True)
                    return
                if action == "reject":
                    payment.status = "rejected"
                    payment.admin_id = callback.from_user.id
                    payment.processed_at = utcnow()
                    user.payment_status = "rejected"
                    rejected_user_id = user.telegram_id
                elif action == "approve":
                    plan = settings.plans.get(payment.plan_code)
                    if not plan:
                        await callback.answer("Plan configuration missing.", show_alert=True)
                        return
                    _, expires_at = new_expiry(user.premium_expires_at if user.premium_status == "active" else None, plan)
                    try:
                        invite = await create_invite_for_user(bot, settings, user.telegram_id, payment.id, session)
                    except TelegramAPIError as exc:
                        logger.error("Unable to create access invite for payment %s: %s", payment.id, exc)
                        await callback.answer("Invite create failed. Bot admin permissions check karein.", show_alert=True)
                        return
                    payment.status = "approved"
                    payment.admin_id = callback.from_user.id
                    payment.processed_at = utcnow()
                    user.payment_status = "approved"
                    user.active_plan = payment.plan_code
                    user.premium_status = "active"
                    user.premium_started_at = utcnow()
                    user.premium_expires_at = expires_at
                    user.channel_access_status = "invite_sent"
                    approved_user_id = user.telegram_id
                    rejected_user_id = None
                else:
                    await callback.answer("Unknown action.", show_alert=True)
                    return

        if action == "reject":
            try:
                await bot.send_message(rejected_user_id, "Payment rejected, please contact admin @SRKSupports.")
            except TelegramAPIError:
                pass
            await _mark_admin_panel(callback, "❌ Rejected")
            await callback.answer("Rejected")
            return

        assert invite is not None and approved_user_id is not None and expires_at is not None
        try:
            await bot.send_message(
                approved_user_id,
                "Your premium is activated.\n"
                f"Expiry: {expiry_text(expires_at)}\n\n"
                f"Join link {settings.invite_valid_minutes} minutes tak valid hai. Button tap karke join request bhejein.",
                reply_markup=join_keyboard(invite.invite_link),
            )
        except TelegramAPIError as exc:
            logger.warning("Premium approved but invitation could not be delivered to %s: %s", approved_user_id, exc)
            await bot.send_message(callback.from_user.id, "Approved, but user ko invite DM nahi bhej paya. User ne bot block kiya ho sakta hai.")
        await _mark_admin_panel(callback, f"✅ Approved — expires {expiry_text(expires_at)}")
        await callback.answer("Approved")

    async def _mark_admin_panel(callback: CallbackQuery, status: str) -> None:
        if not callback.message:
            return
        old = callback.message.caption or "Payment request"
        if "\n\nResult:" not in old:
            old = f"{old}\n\nResult: {status}"
        try:
            await callback.message.edit_caption(caption=old, reply_markup=None)
        except TelegramBadRequest:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass

    @router.chat_join_request()
    async def handle_join_request(request: ChatJoinRequest, bot: Bot) -> None:
        if request.invite_link is None:
            return
        invite_text = request.invite_link.invite_link
        now = utcnow()
        async with sessions() as session:
            invite = await session.scalar(select(InviteLink).where(InviteLink.invite_link == invite_text))
            if invite is None:
                return
            user = await session.get(User, invite.user_id)
            user_expiry = as_utc(user.premium_expires_at) if user else None
            valid = (
                request.from_user.id == invite.user_id
                and not invite.used
                and not invite.revoked
                and as_utc(invite.expires_at) > now
                and user is not None
                and user.premium_status == "active"
                and user_expiry is not None
                and user_expiry > now
            )
            if valid:
                await bot.approve_chat_join_request(settings.premium_chat_id, request.from_user.id)
                try:
                    await bot.revoke_chat_invite_link(settings.premium_chat_id, invite.invite_link)
                except TelegramBadRequest:
                    pass
                invite.used = True
                invite.revoked = True
                invite.used_at = now
                user.channel_access_status = "joined"
                await session.commit()
                await bot.send_message(request.from_user.id, "Premium channel/group access approved.")
                return
            await bot.decline_chat_join_request(settings.premium_chat_id, request.from_user.id)
            await session.commit()

    @router.message(Command("users"))
    async def list_users(message: Message) -> None:
        if not message.from_user or not is_admin(message.from_user.id):
            return
        async with sessions() as session:
            total = await session.scalar(select(func.count()).select_from(User))
            result = await session.execute(select(User).order_by(User.created_at.desc()).limit(20))
            users = result.scalars().all()
        lines = [f"Total users: {total}", "Latest 20 users:"]
        for user in users:
            handle = f"@{user.username}" if user.username else "—"
            lines.append(f"{user.telegram_id} | {handle} | {user.premium_status}")
        await message.answer("\n".join(lines))

    @router.message(Command("premium_users"))
    async def list_premium_users(message: Message) -> None:
        if not message.from_user or not is_admin(message.from_user.id):
            return
        now = utcnow()
        async with sessions() as session:
            result = await session.execute(
                select(User).where(User.premium_status == "active", User.premium_expires_at > now).order_by(User.premium_expires_at)
            )
            users = result.scalars().all()
        if not users:
            await message.answer("No active premium users.")
            return
        lines = [f"Active premium users: {len(users)}"]
        for user in users[:40]:
            lines.append(f"{user.telegram_id} | {user.active_plan or 'manual'} | {expiry_text(user.premium_expires_at)}")
        await message.answer("\n".join(lines))

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not message.from_user or not is_admin(message.from_user.id):
            return
        async with sessions() as session:
            users_total = await session.scalar(select(func.count()).select_from(User))
            active = await session.scalar(select(func.count()).select_from(User).where(User.premium_status == "active"))
            expired = await session.scalar(select(func.count()).select_from(User).where(User.premium_status == "expired"))
            pending = await session.scalar(select(func.count()).select_from(PaymentRequest).where(PaymentRequest.status == "pending"))
            approved = await session.scalar(select(func.count()).select_from(PaymentRequest).where(PaymentRequest.status == "approved"))
            rejected = await session.scalar(select(func.count()).select_from(PaymentRequest).where(PaymentRequest.status == "rejected"))
        await message.answer(
            "Bot stats\n"
            f"Users: {users_total}\n"
            f"Active premium: {active}\n"
            f"Expired: {expired}\n"
            f"Payments pending: {pending}\n"
            f"Payments approved: {approved}\n"
            f"Payments rejected: {rejected}"
        )

    @router.message(Command("addpremium"))
    async def add_premium(message: Message, command: CommandObject, bot: Bot) -> None:
        if not message.from_user or not is_admin(message.from_user.id):
            return
        parts = (command.args or "").split()
        if len(parts) != 2:
            await message.answer("Usage: /addpremium user_id days")
            return
        try:
            target_id, days = int(parts[0]), int(parts[1])
            if days <= 0:
                raise ValueError
        except ValueError:
            await message.answer("User ID aur days valid positive numbers hone chahiye.")
            return
        async with sessions() as session:
            user = await session.get(User, target_id)
            if user is None:
                user = User(telegram_id=target_id)
                session.add(user)
            now = utcnow()
            current = as_utc(user.premium_expires_at)
            base = current if user.premium_status == "active" and current and current > now else now
            user.selected_plan = f"manual-{days}d"
            user.active_plan = f"manual-{days}d"
            user.payment_status = "admin_added"
            user.premium_status = "active"
            user.premium_started_at = now
            user.premium_expires_at = base + timedelta(days=days)
            user.channel_access_status = "invite_sent"
            invite = await create_invite_for_user(bot, settings, target_id, None, session)
            expiry = user.premium_expires_at
            await session.commit()
        try:
            await bot.send_message(
                target_id,
                "Your premium is activated.\n"
                f"Expiry: {expiry_text(expiry)}\n\n"
                f"Join link {settings.invite_valid_minutes} minutes tak valid hai.",
                reply_markup=join_keyboard(invite.invite_link),
            )
            await message.answer(f"Premium added for {target_id} until {expiry_text(expiry)}.")
        except TelegramForbiddenError:
            await message.answer("Premium added, lekin user ko DM nahi ja saka. User ko pehle bot /start karna hoga.")

    @router.message(Command("removepremium"))
    async def remove_premium(message: Message, command: CommandObject, bot: Bot) -> None:
        if not message.from_user or not is_admin(message.from_user.id):
            return
        try:
            target_id = int((command.args or "").strip())
        except ValueError:
            await message.answer("Usage: /removepremium user_id")
            return
        async with sessions() as session:
            user = await session.get(User, target_id)
            if user is None:
                await message.answer("User not found.")
                return
            try:
                await bot.ban_chat_member(settings.premium_chat_id, target_id)
            except TelegramAPIError as exc:
                logger.warning("Unable to remove %s: %s", target_id, exc)
            links = (await session.execute(select(InviteLink).where(InviteLink.user_id == target_id, InviteLink.revoked.is_(False)))).scalars().all()
            for invite in links:
                try:
                    await bot.revoke_chat_invite_link(settings.premium_chat_id, invite.invite_link)
                except TelegramBadRequest:
                    pass
                invite.revoked = True
            user.premium_status = "removed"
            user.channel_access_status = "removed"
            user.premium_expires_at = utcnow()
            await session.commit()
        try:
            await bot.send_message(target_id, "Your premium access has been removed. Please contact admin @SRKSupports.")
        except TelegramAPIError:
            pass
        await message.answer(f"Premium removed for {target_id}.")

    @router.message(Command("broadcast"))
    async def broadcast(message: Message, command: CommandObject, bot: Bot) -> None:
        if not message.from_user or not is_admin(message.from_user.id):
            return
        text = (command.args or "").strip()
        if not text:
            await message.answer("Usage: /broadcast message")
            return
        async with sessions() as session:
            user_ids = list((await session.execute(select(User.telegram_id))).scalars().all())
        sent = 0
        failed = 0
        for user_id in user_ids:
            try:
                await bot.send_message(user_id, text)
                sent += 1
            except TelegramAPIError:
                failed += 1
        await message.answer(f"Broadcast completed. Sent: {sent}, Failed: {failed}")

    return router
