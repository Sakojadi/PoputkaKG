from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from bot.config import config

jobstores = {
    'default': SQLAlchemyJobStore(url=config.database_url.replace('+aiosqlite', '').replace('+asyncpg', ''))
}

scheduler = AsyncIOScheduler(jobstores=jobstores)

def start_scheduler():
    scheduler.start()
