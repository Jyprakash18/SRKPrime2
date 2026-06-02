from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Plan


def plans_keyboard(plans: dict[str, Plan]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{plan.name} — {plan.amount}", callback_data=f"plan:{plan.code}")]
        for plan in plans.values()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def approval_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Approve", callback_data=f"pay:approve:{payment_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"pay:reject:{payment_id}"),
            ]
        ]
    )


def join_keyboard(invite_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Join Premium Access", url=invite_link)]]
    )
