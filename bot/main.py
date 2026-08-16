import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from bot.config import config
from bot.handlers import get_handlers_router
from bot.database.db import init_db
from bot.services.scheduler import start_scheduler

async def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    
    # Initialize DB
    await init_db()
    
    # Start Scheduler
    start_scheduler()
    
    bot = Bot(token=config.bot_token)
    dp = Dispatcher()

    dp.include_router(get_handlers_router())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
