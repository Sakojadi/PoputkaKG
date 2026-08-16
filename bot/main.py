import asyncio
import logging
import sys
import uvicorn

from aiogram import Bot, Dispatcher
from bot.config import config
from bot.handlers import get_handlers_router
from bot.database.db import init_db
from bot.services.scheduler import start_scheduler
from bot.web.app import app as fastapi_app
from bot.middlewares import BanMiddleware

async def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    logger = logging.getLogger("main")
    
    # Initialize DB
    await init_db()
    
    # Start Scheduler
    start_scheduler()
    
    bot = Bot(token=config.bot_token)
    
    # Explicitly remove Telegram command menu button
    try:
        await bot.delete_my_commands()
    except Exception as e:
        logging.debug(f"Failed to delete commands: {e}")
        
    dp = Dispatcher()
    
    # Register global BanMiddleware on outer middleware (intercepts before any handlers)
    ban_middleware = BanMiddleware()
    dp.message.outer_middleware(ban_middleware)
    dp.callback_query.outer_middleware(ban_middleware)
    
    dp.include_router(get_handlers_router())

    # Configure Uvicorn Web Server
    uvi_config = uvicorn.Config(
        app=fastapi_app,
        host="0.0.0.0",
        port=config.port,
        log_level="warning",
        loop="asyncio"
    )
    server = uvicorn.Server(uvi_config)

    logger.info(f"Database dialect: {config.database_url.split('://')[0]}")
    logger.info(f"Starting Bot & Web Admin Panel concurrently on port {config.port}...")

    # Run bot polling and web server concurrently in the same event loop
    await asyncio.gather(
        dp.start_polling(bot),
        server.serve()
    )

if __name__ == "__main__":
    asyncio.run(main())
