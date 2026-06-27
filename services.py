from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from dateutil.relativedelta import relativedelta
# 🔴 SQL imports hata diye hain

from config import Plan, Settings

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
    payment_request_id: str | None,  # 🟢 MongoDB mein IDs string ya ObjectId hoti hain
    db,                             # 🔄 session ki jagah ab 'db' receive karega
) -> dict:
    chat_ids = settings.premium_chat_id if isinstance(settings.premium_chat_id, list) else [settings.premium_chat_id]
    
    for cid in chat_ids:
        try:
            await bot.unban_chat_member(cid, user_id, only_if_banned=True)
        except TelegramBadRequest:
            logger.debug("User %s did not require unban before invitation in chat %s", user_id, cid)

    invite_expires = utcnow() + timedelta(minutes=settings.invite_valid_minutes)
    
    # Pehle chat_id par link generate karte hain (default entry ke liye)
    primary_chat_id = chat_ids[0]
    telegram_link = await bot.create_chat_invite_link(
        chat_id=primary_chat_id,
        name=f"premium-{user_id}",
        expire_date=invite_expires,
        creates_join_request=True,
    )
    
    # 🔄 MongoDB Document Format
    invite_data = {
        "user_id": user_id,
        "payment_request_id": payment_request_id,
        "invite_link": telegram_link.invite_link,
        "expires_at": invite_expires,
        "used": False,
        "revoked": False,
        "created_at": utcnow()
    }
    
    await db.invite_links.insert_one(invite_data)
    return invite_data


async def expire_due_users(
    bot: Bot,
    settings: Settings,
    db,  # 🔄 sessions (SQL) ki jagah ab 'db' (MongoDB) receive karega
) -> int:
    now = utcnow()
    removed = 0
    
    # 🔄 MongoDB Query: Un active users ko dhoondo jinki expiry 'now' se kam ya barabar hai
    cursor = db.users.find({
        "premium_status": "active",
        "premium_expires_at": {"$ne": None, "$lte": now}
    })
    users = await cursor.to_
