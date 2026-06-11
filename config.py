from __future__ import annotations

from dataclasses import dataclass
from os import getenv
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True, slots=True)
class Plan:
    code: str
    name: str
    months: int
    amount: str


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    premium_chat_id: int | str
    database_url: str
    webhook_base_url: str
    webhook_path: str
    webhook_secret: str
    cron_secret: str
    support_text: str
    invite_valid_minutes: int
    expiry_check_seconds: int
    plans: dict[str, Plan]

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_base_url.rstrip('/')}{self.webhook_path}"

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = _required("BOT_TOKEN")
        admin_ids = frozenset(
            int(item.strip())
            for item in _required("ADMIN_IDS").split(",")
            if item.strip()
        )
        if not admin_ids:
            raise ValueError("ADMIN_IDS must include at least one Telegram numeric ID")

        premium_chat_id = _parse_chat_id(_required("PREMIUM_CHAT_ID"))
        webhook_base_url = _required("WEBHOOK_BASE_URL").rstrip("/")
        parsed_url = urlparse(webhook_base_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ValueError("WEBHOOK_BASE_URL must be an HTTPS URL, for example https://your-app.onrender.com")

        plans = {
            "1m": Plan("1m", "1 Month", 1, getenv("PLAN_1M_AMOUNT", "₹99")),
            "3m": Plan("3m", "3 Months", 3, getenv("PLAN_3M_AMOUNT", "₹149")),
            "6m": Plan("6m", "6 Months", 6, getenv("PLAN_6M_AMOUNT", "₹269")),
            "12m": Plan("12m", "1 Year", 12, getenv("PLAN_12M_AMOUNT", "₹499")),
        }
        return cls(
            bot_token=bot_token,
            admin_ids=admin_ids,
            premium_chat_id=premium_chat_id,
            database_url=getenv("DATABASE_URL", "sqlite+aiosqlite:///premium_bot.db"),
            webhook_base_url=webhook_base_url,
            webhook_path=getenv("WEBHOOK_PATH", "/telegram/webhook"),
            webhook_secret=_required("WEBHOOK_SECRET"),
            cron_secret=_required("CRON_SECRET"),
            support_text=getenv("SUPPORT_TEXT", "Please contact admin for support."),
            invite_valid_minutes=int(getenv("INVITE_VALID_MINUTES", "60")),
            expiry_check_seconds=int(getenv("EXPIRY_CHECK_SECONDS", "300")),
            plans=plans,
        )


def _required(name: str) -> str:
    value = getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _parse_chat_id(value):
    if not value:
        return []
    
    # Split the string by commas if there are multiple IDs
    id_list = [item.strip() for item in value.split(',')]
    parsed_ids = []
    
    for item in id_list:
        try:
            parsed_ids.append(int(item))
        except ValueError:
            if item.startswith('@'):
                parsed_ids.append(item)
            else:
                raise ValueError("PREMIUM_CHAT_ID must contain numeric chat IDs or @channelusernames separated by commas")
                
    return parsed_ids
