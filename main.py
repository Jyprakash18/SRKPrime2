from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from motor.motor_asyncio import AsyncIOMotorClient  # 🟢 Naya MongoDB import
from config import Settings
# from db import build_database, init_db  # 🔴 SQL waali line hata di
from handlers import make_router
from services import expire_due_users

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "telegram-premium-bot"})


async def run_expiry_endpoint(request: web.Request) -> web.Response:
    expected = f"Bearer {request.app['settings'].cron_secret}"
    if request.headers.get("Authorization") != expected:
        raise web.HTTPUnauthorized(text="Invalid scheduler secret")
    # 🔄 sessions ki jagah ab 'db' pass hoga
    removed = await expire_due_users(request.app["bot"], request.app["settings"], request.app["db"])
    return web.json_response({"ok": True, "expired_removed": removed})


async def expiry_loop(app: web.Application) -> None:
    settings: Settings = app["settings"]
    while True:
        try:
            # 🔄 sessions ki jagah ab 'db' pass hoga
            removed = await expire_due_users(app["bot"], settings, app["db"])
            if removed:
                logger.info("Removed %s expired premium member(s)", removed)
        except Exception:
            logger.exception("Expiry worker failed")
        await asyncio.sleep(settings.expiry_check_seconds)


async def on_startup(app: web.Application) -> None:
    settings: Settings = app["settings"]
    bot: Bot = app["bot"]
    
    # await init_db(app["engine"])  # 🔴 SQL database init hata diya (Mongo mein zaroorat nahi)
    
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Show premium plans"),
            BotCommand(command="myplan", description="View current plan"),
            BotCommand(command="renew", description="Renew premium"),
            BotCommand(command="help", description="Get support"),
        ]
    )
    await bot.set_webhook(
        settings.webhook_url,
        secret_token=settings.webhook_secret,
        allowed_updates=app["dispatcher"].resolve_used_update_types(),
    )
    app["expiry_task"] = asyncio.create_task(expiry_loop(app))
    logger.info("Webhook configured at %s", settings.webhook_url)


async def on_cleanup(app: web.Application) -> None:
    task = app.get("expiry_task")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    app["mongo_client"].close()  # 🟢 SQL dispose ki jagah MongoDB client close kiya


def create_app() -> web.Application:
    settings = Settings.from_env()
    
    # 🟢 MongoDB Connection Setup
    mongo_client = AsyncIOMotorClient(settings.mongo_uri)
    db = mongo_client["srk_prime_db"]  # Aapka database naam
    
    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    
    # 🔄 Router ko ab sessions ki jagah 'db' pass kar rahe hain
    dispatcher.include_router(make_router(settings, db))

    app = web.Application()
    app["settings"] = settings
    app["mongo_client"] = mongo_client  # Cleanup ke liye save kiya
    app["db"] = db                      # Sessions ki jagah ab Pure app mein 'db' use hoga
    app["bot"] = bot
    app["dispatcher"] = dispatcher

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/tasks/expire", run_expiry_endpoint)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        handle_in_background=True,
        secret_token=settings.webhook_secret,
    ).register(app, path=settings.webhook_path)
    setup_application(app, dispatcher, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
