import os
import pathlib
import tempfile

# Must run before any `bot.*` import: bot/config.py builds Settings() and
# bot/database/db.py builds the engine at module import time.
_TEST_DB = pathlib.Path(tempfile.gettempdir()) / "poputka_test.db"
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB}"
os.environ["XPAY_CLIENT_ID"] = "test-client-id"
os.environ["XPAY_CLIENT_SECRET"] = "test-client-secret"
os.environ["XPAY_MODE"] = "sandbox"
os.environ.pop("PUBLIC_BASE_URL", None)
os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)

import pytest

import bot.database.models  # noqa: F401
from bot.database.db import Base, engine


@pytest.fixture
async def db():
    """A clean schema per test, on a throwaway sqlite file."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def reset_xpay_state():
    """Clear the module-level token/client cache between tests."""
    from bot.services import xpay

    xpay._client = None
    xpay._token = None
    xpay._service_uuid = None
    xpay._expires_at = None
    yield
    xpay._client = None
    xpay._token = None
    xpay._service_uuid = None
    xpay._expires_at = None
