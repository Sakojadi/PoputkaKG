import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from bot.config import config

logger = logging.getLogger(__name__)

Base = declarative_base()

engine = create_async_engine(config.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db():
    # 1. Create all base tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Base tables created/verified successfully.")

    # 2. Run column migrations in separate transactions
    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP",
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS price_paid FLOAT DEFAULT 0.0",
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS last_message_id BIGINT",
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS message_ids TEXT DEFAULT ''",
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS failure_count INTEGER DEFAULT 0",
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS created_at TIMESTAMP",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS campaign_id INTEGER",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
    ]

    for sql in migrations:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        except Exception:
            # Fallback for SQLite which doesnt support IF NOT EXISTS in ADD COLUMN
            if "IF NOT EXISTS" in sql:
                clean_sql = sql.replace(" IF NOT EXISTS", "")
                try:
                    async with engine.begin() as conn:
                        await conn.execute(text(clean_sql))
                except Exception:
                    pass
