import asyncio
import logging
import sys

import uvicorn

from bot.config import config
from bot.database.db import init_db
from bot.dispatcher import build_dispatcher
from bot.handlers.group_moderation import ensure_group_keyboard_removed
from bot.services.fsm import set_dispatcher
from bot.services.group_filter import reload_soft_words
from bot.services.moderation import reload_banned_words
from bot.services.scheduler import start_scheduler, sync_jobs_with_db
from bot.services.settings_store import load_settings
from bot.services.tg import close_bot, get_bot
from bot.services.xpay import close_client as close_xpay_client
from bot.web.app import app as fastapi_app


async def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    logger = logging.getLogger("main")

    if config.payment_provider == "mock":
        logger.warning(
            "PAYMENT_PROVIDER=mock: payments are NOT charged or verified. "
            "Set PAYMENT_PROVIDER=xpay to take real money."
        )
    else:
        logger.info(f"Payments: xPay ({config.xpay_mode})")

    # Initialize DB
    await init_db()
    await load_settings()
    await reload_banned_words()
    await reload_soft_words()

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

    # One-time: remove the stale reply keyboard an old /start left in the group.
    await ensure_group_keyboard_removed(bot)

    dp = build_dispatcher()

    # The xPay webhook runs in the web layer and needs to move a paying
    # user's FSM state, so the dispatcher must be reachable from there.
    set_dispatcher(dp)

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
            dp.start_polling(
                bot,
                handle_signals=False,
                close_bot_session=False,
                # Only these have handlers; anything else is noise. Edits are
                # needed so a clean group post can't be edited into spam.
                allowed_updates=["message", "edited_message", "callback_query"],
            ),
            server.serve(),
        )
    finally:
        await close_xpay_client()
        await close_bot()


if __name__ == "__main__":
    asyncio.run(main())
