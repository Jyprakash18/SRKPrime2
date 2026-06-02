from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Plan, Settings
from .models import InviteLink, User

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def new_expiry(existing: datetime | None, plan: Plan) -> tuple[datetime, datetime]:
    now = utcnow()
    current = as_utc(existing)
    start_from = current if current and current > now else now
    return now, start_from + relativedelta(months=plan.months)


async def create_invite_for_user(
    bot: Bot,
    settings: Settings,
    user_id: int,
    payment_request_id: int | None,
    session: AsyncSession,
) -> InviteLink:
    try:
        await bot.unban_chat_member(settings.premium_chat_id, user_id, only_if_banned=True)
    except TelegramBadRequest:
        # Telegram may report that there is nothing to unban; a new user can still join.
        logger.debug("User %s did not require unban before invitation", user_id)

    invite_expires = utcnow() + timedelta(minutes=settings.invite_valid_minutes)
    telegram_link = await bot.create_chat_invite_link(
        chat_id=settings.premium_chat_id,
        name=f"premium-{user_id}",
        expire_date=invite_expires,
        creates_join_request=True,
    )
    invite = InviteLink(
        user_id=user_id,
        payment_request_id=payment_request_id,
        invite_link=telegram_link.invite_link,
        expires_at=invite_expires,
    )
    session.add(invite)
    return invite


async def expire_due_users(
    bot: Bot,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
) -> int:
    now = utcnow()
    removed = 0
    async with sessions() as session:
        result = await session.execute(
            select(User).where(
                User.premium_status == "active",
                User.premium_expires_at.is_not(None),
                User.premium_expires_at <= now,
            )
        )
        users = result.scalars().all()
        for user in users:
            open_links_result = await session.execute(
                select(InviteLink).where(
                    InviteLink.user_id == user.telegram_id,
                    InviteLink.used.is_(False),
                    InviteLink.revoked.is_(False),
                )
            )
            for invite in open_links_result.scalars().all():
                try:
                    await bot.revoke_chat_invite_link(settings.premium_chat_id, invite.invite_link)
                except TelegramBadRequest:
                    pass
                invite.revoked = True

            try:
                await bot.ban_chat_member(settings.premium_chat_id, user.telegram_id)
            except TelegramBadRequest as exc:
                logger.warning("Could not ban/remove expired user %s: %s", user.telegram_id, exc)
            user.premium_status = "expired"
            user.channel_access_status = "removed"
            removed += 1
            try:
                await bot.send_message(
                    user.telegram_id,
                    "Your premium has expired. Use /renew to choose a new plan.",
                )
            except TelegramForbiddenError:
                logger.info("Could not notify expired user %s; bot is blocked", user.telegram_id)
        await session.commit()
    return removed
