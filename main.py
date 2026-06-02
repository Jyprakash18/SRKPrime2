from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from .config import Settings
from .db import build_database, init_db
from .handlers import make_router
from .services import expire_due_users

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
    removed = await expire_due_users(request.app["bot"], request.app["settings"], request.app["sessions"])
    return web.json_response({"ok": True, "expired_removed": removed})


async def expiry_loop(app: web.Application) -> None:
    settings: Settings = app["settings"]
    while True:
        try:
            removed = await expire_due_users(app["bot"], settings, app["sessions"])
            if removed:
                logger.info("Removed %s expired premium member(s)", removed)
        except Exception:
            logger.exception("Expiry worker failed")
        await asyncio.sleep(settings.expiry_check_seconds)


async def on_startup(app: web.Application) -> None:
    settings: Settings = app["settings"]
    bot: Bot = app["bot"]
    await init_db(app["engine"])
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
    await app["engine"].dispose()


def create_app() -> web.Application:
    settings = Settings.from_env()
    engine, sessions = build_database(settings.database_url)
    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(settings, sessions))

    app = web.Application()
    app["settings"] = settings
    app["engine"] = engine
    app["sessions"] = sessions
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
