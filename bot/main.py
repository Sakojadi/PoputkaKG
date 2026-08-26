import asyncio
import logging
import sys

import uvicorn
from aiogram import Dispatcher

from bot.config import config
from bot.database.db import init_db
from bot.handlers import get_handlers_router
from bot.middlewares import BanMiddleware
from bot.services.scheduler import start_scheduler, sync_jobs_with_db
from bot.services.tg import close_bot, get_bot
from bot.web.app import app as fastapi_app


async def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    logger = logging.getLogger("main")

    # Initialize DB
    await init_db()

    # Start Scheduler, then drop any jobs left behind by a previous crash and
    # restore jobs for campaigns that are still active.
    start_scheduler()
    await sync_jobs_with_db()

    bot = get_bot()

    # Explicitly remove Telegram command menu button
    try:
        await bot.delete_my_commands()
    except Exception as e:
        logger.debug(f"Failed to delete commands: {e}")

    dp = Dispatcher()

    # Register global BanMiddleware
    ban_middleware = BanMiddleware()
    dp.message.outer_middleware(ban_middleware)
    dp.callback_query.outer_middleware(ban_middleware)

    dp.include_router(get_handlers_router())

    # Configure Uvicorn Web Server
    uvi_config = uvicorn.Config(
        app=fastapi_app,
        host="0.0.0.0",
        port=config.port,
        log_level="info",
        access_log=True,
    )
    server = uvicorn.Server(uvi_config)

    logger.info(f"Database dialect: {config.database_url.split('://')[0]}")
    logger.info(f"Starting Bot & Web Admin Panel on 0.0.0.0:{config.port}...")

    # Run bot polling and web server concurrently
    try:
        # close_bot_session=False: the Bot is a process-wide singleton shared with
        # the scheduler and the web admin, so its lifecycle belongs to close_bot()
        # below, not to whichever coroutine happens to finish first.
        await asyncio.gather(
            dp.start_polling(bot, handle_signals=False, close_bot_session=False),
            server.serve(),
        )
    finally:
        await close_bot()


if __name__ == "__main__":
    asyncio.run(main())
