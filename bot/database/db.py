import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
        migrations = [
            "ALTER TABLE users ADD COLUMN username VARCHAR",
            "ALTER TABLE users ADD COLUMN is_banned BOOLEAN DEFAULT 0",
            "ALTER TABLE users ADD COLUMN created_at TIMESTAMP",
            "ALTER TABLE campaigns ADD COLUMN price_paid FLOAT DEFAULT 0.0",
            "ALTER TABLE campaigns ADD COLUMN last_message_id BIGINT",
            "ALTER TABLE campaigns ADD COLUMN message_ids TEXT DEFAULT ''",
            "ALTER TABLE campaigns ADD COLUMN created_at TIMESTAMP"
        ]
        
        for sql in migrations:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass
