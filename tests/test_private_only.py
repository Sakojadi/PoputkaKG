import importlib.util

from sqlalchemy import func, select

from bot.config import Settings
from bot.database.db import AsyncSessionLocal
from bot.database.models import User
from bot.locales.translations import TEXTS
from tests.tg_fakes import group_update, private_update


async def test_ad_button_in_group_gets_no_reply(tg):
    await tg.feed(group_update(TEXTS["ky"]["ad_button"]))
    assert tg.session.sent("SendMessage") == []


async def test_start_command_in_group_gets_no_reply(tg):
    await tg.feed(group_update("/start"))
    assert tg.session.sent("SendMessage") == []


async def test_group_message_creates_no_user_row(tg):
    await tg.feed(group_update("Бишкекке жолго чыгам"))
    async with AsyncSessionLocal() as session:
        total = (await session.execute(select(func.count()).select_from(User))).scalar()
    assert total == 0


async def test_banned_user_gets_no_reply_in_group(tg):
    async with AsyncSessionLocal() as session:
        session.add(User(id=777, is_banned=True))
        await session.commit()
    await tg.feed(group_update("hello"))
    assert tg.session.sent("SendMessage") == []


async def test_private_start_still_answers(tg):
    await tg.feed(private_update("/start"))
    sent = tg.session.sent("SendMessage")
    assert len(sent) == 1
    assert sent[0]["chat_id"] == 777


def test_admin_ids_setting_removed():
    assert "admin_ids" not in Settings.model_fields
    assert importlib.util.find_spec("bot.handlers.admin") is None
