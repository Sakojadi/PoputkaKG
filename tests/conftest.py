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
    from bot.services import settings_store

    # The settings cache is process-wide; dropping the tables must not leave
    # stale values (e.g. a group_filter_mode) behind for the next test.
    settings_store._cache.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    settings_store._cache.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(scope="session")
def dispatcher():
    """The real dispatcher. Routers are module-level singletons, so it can only
    be built once per process."""
    from bot.dispatcher import build_dispatcher

    return build_dispatcher()


@pytest.fixture
async def tg(db, dispatcher):
    """Feed updates to the real handlers with a fake Bot API behind them."""
    from types import SimpleNamespace

    from tests.tg_fakes import make_bot

    bot, session = make_bot()

    async def feed(update):
        await dispatcher.feed_update(bot, update)

    yield SimpleNamespace(bot=bot, session=session, feed=feed)
    dispatcher.storage.storage.clear()  # don't leak FSM state between tests


@pytest.fixture
def group_chat():
    """Point GROUP_ID at the fake group.

    Patches the `config` object the handler module holds, not
    `bot.config.config`: tests/test_config.py reloads bot.config, after which
    the two are different objects.
    """
    from bot.handlers.group_moderation import config
    from tests.tg_fakes import GROUP_ID

    previous = config.group_id
    config.group_id = GROUP_ID
    yield GROUP_ID
    config.group_id = previous


@pytest.fixture
def use_xpay():
    """Select the real xPay provider for a test that mocks its HTTP calls.

    The shipped default is the mock provider, so anything exercising the xPay
    code path has to ask for it explicitly.
    """
    import bot.config

    previous = bot.config.config.payment_provider
    bot.config.config.payment_provider = "xpay"
    yield
    bot.config.config.payment_provider = previous


@pytest.fixture
def use_mock_payments():
    import bot.config

    previous = bot.config.config.payment_provider
    bot.config.config.payment_provider = "mock"
    yield
    bot.config.config.payment_provider = previous


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
