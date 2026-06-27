from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo
from bson import ObjectId  # 🟢 MongoDB IDs handle karne ke liye naya import

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, ChatJoinRequest, Message, User as TgUser, InlineKeyboardMarkup, InlineKeyboardButton

from config import Settings
from keyboards import approval_keyboard, join_keyboard, plans_keyboard
# 🔴 SQL waale Models hata diye kyunki Mongo mein models ki direct zaroorat nahi hoti
from services import as_utc, create_invite_for_user, new_expiry, utcnow

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


# 🔄 sessions ki jagah ab 'db' receive karenge
def make_router(settings: Settings, db) -> Router:
    router = Router(name="premium_bot")

    # 🟢 MongoDB Upsert Method
    async def upsert_user(telegram_user: TgUser) -> dict:
        user = await db.users.find_one_and_update(
            {"_id": telegram_user.id},  # Telegram ID ko primary key (_id) banaya
            {"$set": {
                "username": telegram_user.username,
                "full_name": telegram_user.full_name,
            }},
            upsert=True,
            return_document=True
        )
        return user

    def is_admin(user_id: int) -> bool:
        return user_id in settings.admin_ids

    def expiry_text(value) -> str:
        if not value:
            return "—"
        value = as_utc(value)
        return value.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not message.from_user:
            return
        
        # 🔄 SQL sessions ki jagah MongoDB Upsert
        await upsert_user(message.from_user)
        
        await message.answer(
            text="Welcome! Choose your premium plan, complete the payment, and upload your payment screenshot. After admin approval, you will get access to the premium and group:",
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
        
        # 🔄 MongoDB Update
        await db.users.update_one(
            {"_id": callback.from_user.id},
            {"$set": {
                "username": callback.from_user.username,
                "full_name": callback.from_user.full_name,
                "selected_plan": plan.code,
                "payment_status": "awaiting_screenshot"
            }},
            upsert=True
        )
        
        text = (
            f"Plan: {plan.name}\n"
            f"Amount: {plan.amount}\n\n"
            "Thank You! Once your payment is completed, kindly upload the payment screenshot here for verification.\n"
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
        
        # 🔄 MongoDB Find One
        user = await db.users.find_one({"_id": message.from_user.id})
        
        if user is None or user.get("premium_status") != "active":
            await message.answer("Aapka koi active premium plan nahi hai. /start se plan choose karein.")
            return
            
        plan = settings.plans.get(user.get("active_plan") or "")
        name = plan.name if plan else (user.get("active_plan") or "Premium")
        await message.answer(
            f"Current plan: {name}\n"
            f"Status: Active\n"
            f"Expiry: {expiry_text(user.get('premium_expires_at'))}\n"
            f"Access: {user.get('channel_access_status')}"
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(settings.support_text)

    @router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
    async def receive_screenshot(message: Message, bot: Bot) -> None:
        if not message.from_user:
            return
            
        user = await upsert_user(message.from_user)
        if not user.get("selected_plan") or user.get("selected_plan") not in settings.plans:
            await message.answer("Pehle /start se premium plan choose karein, phir screenshot bhejein.")
            return
            
        # 🔄 Existing Request Check via MongoDB
        existing = await db.payment_requests.find_one({
            "user_id": user["_id"],
            "status": "pending"
        })
        if existing:
            await message.answer("Aapka screenshot already approval ke liye pending hai.")
            return
            
        plan = settings.plans[user["selected_plan"]]
        if message.photo:
            file_id = message.photo[-1].file_id
            kind = "photo"
        elif message.document:
            file_id = message.document.file_id
            kind = "document"
        else:
            return
            
        # 🔄 MongoDB Insert Request
        request_data = {
            "user_id": user["_id"],
            "plan_code": plan.code,
            "plan_name": plan.name,
            "amount": plan.amount,
            "screenshot_file_id": file_id,
            "screenshot_kind": kind,
            "status": "pending",
            "created_at": utcnow()
        }
        res = await db.payment_requests.insert_one(request_data)
        request_id = str(res.inserted_id)  # MongoDB _id ko string mein convert kiya admin panel ke liye
        
        await db.users.update_one({"_id": user["_id"]}, {"$set": {"payment_status": "pending"}})

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
            request_id = ObjectId(request_text)  # String ID ko MongoDB ObjectId mein convert kiya
        except Exception:
            await callback.answer("Invalid action or Request ID.", show_alert=True)
            return

        expires_at = None
        invite_links = []
        
        # 🟢 [RACE CONDITION FIX]: Atomic tarike se status pending se approved/rejected karenge takki double click se crash na ho
        payment = await db.payment_requests.find_one_and_update(
            {"_id": request_id, "status": "pending"},
            {"$set": {
                "status": "approved" if action == "approve" else "rejected",
                "admin_id": callback.from_user.id,
                "processed_at": utcnow()
            }},
            return_document=True
        )
        
        if payment is None:
            # Agar request pehle hi process ho chuki hai
            existing_pay = await db.payment_requests.find_one({"_id": request_id})
            if existing_pay is None:
                await callback.answer("Payment request not found.", show_alert=True)
            else:
                await callback.answer(f"Already {existing_pay.get('status')}.", show_alert=True)
            return
            
        user = await db.users.find_one({"_id": payment["user_id"]})
        if user is None:
            await callback.answer("User not found.", show_alert=True)
            return

        if action == "reject":
            await db.users.update_one({"_id": payment["user_id"]}, {"$set": {"payment_status": "rejected"}})
            try:
                await bot.send_message(payment["user_id"], "Payment rejected, please contact admin @SRKSupports.")
            except TelegramAPIError:
                pass
            await _mark_admin_panel(callback, "❌ Rejected")
            await callback.answer("Rejected")
            return

        elif action == "approve":
            plan = settings.plans.get(payment["plan_code"])
            if not plan:
                await callback.answer("Plan configuration missing.", show_alert=True)
                return
                
            user_current_expiry = user.get("premium_expires_at") if user.get("premium_status") == "active" else None
            _, expires_at = new_expiry(user_current_expiry, plan)
            
            chat_ids = settings.premium_chat_id if isinstance(settings.premium_chat_id, list) else [settings.premium_chat_id]
            for cid in chat_ids:
                try:
                    expire_date = utcnow() + timedelta(minutes=settings.invite_valid_minutes)
                    tg_invite = await bot.create_chat_invite_link(
                        chat_id=cid,
                        creates_join_request=True,
                        expire_date=expire_date
                    )
                    
                    # 🔄 Invite Link ko MongoDB mein save karein
                    await db.invite_links.insert_one({
                        "user_id": user["_id"],
                        "invite_link": tg_invite.invite_link,
                        "expires_at": expire_date,
                        "used": False,
                        "revoked": False,
                        "created_at": utcnow()
                    })
                    invite_links.append((cid, tg_invite.invite_link))
                except TelegramAPIError as exc:
                    logger.error("Unable to create access invite for chat %s: %s", cid, exc)
            
            if not invite_links:
                await callback.answer("Invite links generation failed. Bot admin permissions check karein.", show_alert=True)
                return

            # 🔄 User Status MongoDB mein update karein
            await db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {
                    "payment_status": "approved",
                    "active_plan": payment["plan_code"],
                    "premium_status": "active",
                    "premium_started_at": utcnow(),
                    "premium_expires_at": expires_at,
                    "channel_access_status": "invite_sent"
                }}
            )

        buttons = []
        for cid, link in invite_links:
            try:
                chat_info = await bot.get_chat(cid)
                title = chat_info.title
            except Exception:
                title = "Premium Chat"
            buttons.append([InlineKeyboardButton(text=f"Join {title}", url=link)])
        custom_join_markup = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            await bot.send_message(
                payment["user_id"],
                "Your premium is activated.\n"
                f"Expiry: {expiry_text(expires_at)}\n\n"
                f"Join links {settings.invite_valid_minutes} minutes tak valid hain. Buttons par tap karke join requests bhejein.",
                reply_markup=custom_join_markup,
            )
        except TelegramAPIError as exc:
            logger.warning("Premium approved but invitation could not be delivered to %s: %s", payment["user_id"], exc)
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
        
        # 🔄 MongoDB Invite Check
        invite = await db.invite_links.find_one({"invite_link": invite_text})
        if invite is None:
            return
            
        user = await db.users.find_one({"_id": invite["user_id"]})
        user_expiry = as_utc(user.get("premium_expires_at")) if user else None
        
        valid = (
            request.from_user.id == invite["user_id"]
            and not invite.get("used")
            and not invite.get("revoked")
            and as_utc(invite.get("expires_at")) > now
            and user is not None
            and user.get("premium_status") == "active"
            and user_expiry is not None
            and user_expiry > now
        )
        
        if valid:
            await bot.approve_chat_join_request(request.chat.id, request.from_user.id)
            try:
                await bot.revoke_chat_invite_link(request.chat.id, invite["invite_link"])
            except TelegramBadRequest:
                pass
                
            # 🔄 Update Invite aur User status MongoDB mein
            await db.invite_links.update_one(
                {"_id": invite["_id"]},
                {"$set": {"used": True, "revoked": True, "used_at": now}}
            )
            await db.users.update_one(
                {"_id": invite["user_id"]},
                {"$set": {"channel_access_status": "joined"}}
            )
            await bot.send_message(request.from_user.id, "Premium channel/group access approved.")
            return
            
        await bot.decline_chat_join_request(request.chat.id, request.from_user.id)

    @router.message(Command("users"))
    async def list_users(message: Message) -> None:
        if not message.from_user or not is_admin(message.from_user.id):
            return
            
        # 🔄 MongoDB count aur pagination
        total = await db.users.count_documents({})
        cursor = db.users.find().sort("created_at", -1).limit(20)
        users = await cursor.to_list(length=20)
        
        lines = [f"Total users: {total}", "Latest 20 users:"]
        for u in users:
            handle = f"@{u.get('username')}" if u.get('username') else "—"
            lines.append(f"{u['_id']} | {handle} | {u.get('premium_status', 'free')}")
        await message.answer("\n".join(lines))

    @router.message(Command("premium_users"))
    async def list_premium_users(message: Message) -> None:
        if not message.from_user or not is_admin(message.from_user.id):
            return
        now = utcnow()
        
        # 🔄 MongoDB Find query with filters
        cursor = db.users.find({
            "premium_status": "active",
            "premium_expires_at": {"$gt": now}
        }).sort("premium_expires_at", 1).limit(40)
        users = await cursor.to_list(length=40)
        
        if not users:
            await message.answer("No active premium users.")
            return
        lines = [f"Active premium users: {len(users)}"]
        for u in users:
            lines.append(f"{u['_id']} | {u.get('active_plan', 'manual')} | {expiry_text(u.get('premium_expires_at'))}")
        await message.answer("\n".join(lines))

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not message.from_user or not is_admin(message.from_user.id):
            return
            
        # 🔄 MongoDB count queries
        users_total = await db.users.count_documents({})
        active = await db.users.count_documents({"premium_status": "active"})
        expired = await db.users.count_documents({"premium_status": "expired"})
        pending = await db.payment_requests.count_documents({"status": "pending"})
        approved = await db.payment_requests.count_documents({"status": "approved"})
        rejected = await db.payment_requests.count_documents({"status": "rejected"})
        
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
            
        invite_links = []
        user = await db.users.find_one({"_id": target_id})
        now = utcnow()
        current = as_utc(user.get("premium_expires_at")) if user else None
        base = current if user and user.get("premium_status") == "active" and current and current > now else now
        expiry = base + timedelta(days=days)
        
        # 🔄 MongoDB Upsert / Update User
        await db.users.update_one(
            {"_id": target_id},
            {"$set": {
                "selected_plan": f"manual-{days}d",
                "active_plan": f"manual-{days}d",
                "payment_status": "admin_added",
                "premium_status": "active",
                "premium_started_at": now,
                "premium_expires_at": expiry,
                "channel_access_status": "invite_sent"
            }},
            upsert=True
        )
        
        chat_ids = settings.premium_chat_id if isinstance(settings.premium_chat_id, list) else [settings.premium_chat_id]
        for cid in chat_ids:
            try:
                expire_date = utcnow() + timedelta(minutes=settings.invite_valid_minutes)
                tg_invite = await bot.create_chat_invite_link(chat_id=cid, creates_join_request=True, expire_date=expire_date)
                
                # 🔄 Save invite to Mongo
                await db.invite_links.insert_one({
                    "user_id": target_id,
                    "invite_link": tg_invite.invite_link,
                    "expires_at": expire_date,
                    "used": False,
                    "revoked": False,
                    "created_at": utcnow()
                })
                invite_links.append((cid, tg_invite.invite_link))
            except TelegramAPIError as exc:
                logger.error("Manual add: Unable to create invite for chat %s: %s", cid, exc)
                
        buttons = []
        for cid, link in invite_links:
            try:
                chat_info = await bot.get_chat(cid)
                title = chat_info.title
            except Exception:
                title = "Premium Chat"
            buttons.append([InlineKeyboardButton(text=f"Join {title}", url=link)])
        custom_join_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        
        try:
            await bot.send_message(
                target_id,
                "Your premium is activated.\n"
                f"Expiry: {expiry_text(expiry)}\n\n"
                f"Join links {settings.invite_valid_minutes} minutes tak valid hain. Buttons par tap karke join requests bhejein.",
                reply_markup=custom_join_markup,
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
            
        user = await db.users.find_one({"_id": target_id})
        if user is None:
            await message.answer("User not found.")
            return
            
        chat_ids = settings.premium_chat_id if isinstance(settings.premium_chat_id, list) else [settings.premium_chat_id]
        for cid in chat_ids:
            try:
                await bot.ban_chat_member(cid, target_id)
            except TelegramAPIError as exc:
                logger.warning("Unable to remove %s from chat %s: %s", target_id, cid, exc)
                
        # 🔄 Active Invite Links fetch and revoke in MongoDB
        cursor = db.invite_links.find({"user_id": target_id, "revoked": False})
        links = await cursor.to_list(length=None)
        
        for invite in links:
            for cid in chat_ids:
                try:
                    await bot.revoke_chat_invite_link(cid, invite["invite_link"])
                    break
                except TelegramBadRequest:
                    pass
            await db.invite_links.update_one({"_id": invite["_id"]}, {"$set": {"revoked": True}})
            
        # 🔄 Update user in MongoDB
        await db.users.update_one(
            {"_id": target_id},
            {"$set": {
                "premium_status": "removed",
                "channel_access_status": "removed",
                "premium_expires_at": utcnow()
            }}
        )
        
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
            
        # 🔄 MongoDB Users Find All ID
        cursor = db.users.find({}, {"_id": 1})
        users = await cursor.to_list(length=None)
        user_ids = [u["_id"] for u in users]
        
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
