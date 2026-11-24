import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage

from bot.config import config
from bot.handlers import ad_flow
from bot.handlers import group_moderation as gm
from bot.services import group_enforcement as ge
from tests.tg_fakes import GROUP_ID, group_update

SPAM = "FREE HOT GIRL VIDEOS Full video · No blur · Watch now 18+ only — tap button"
SOFT = "ты сука"

pytestmark = pytest.mark.usefixtures("group_chat")


@pytest.fixture(autouse=True)
async def _instant_notices(monkeypatch):
    monkeypatch.setattr(gm, "NOTICE_TTL_SECONDS", 0)
    yield
    await asyncio.gather(*ad_flow._background_tasks)


async def _drain():
    await asyncio.gather(*ad_flow._background_tasks)


def _until(params) -> datetime:
    value = params["until_date"]
    return value if isinstance(value, datetime) else datetime.fromtimestamp(value, UTC)


async def test_hard_first_offence_deletes_and_mutes_for_24_hours(tg):
    await ge.set_mode("enforce")
    await tg.feed(group_update(SPAM, message_id=42))
    await _drain()

    assert {"chat_id": GROUP_ID, "message_id": 42} in tg.session.sent("DeleteMessage")
    (restrict,) = tg.session.sent("RestrictChatMember")
    assert restrict["user_id"] == 777
    assert restrict["permissions"]["can_send_messages"] is False
    expected = datetime.now(UTC) + timedelta(hours=24)
    assert abs(_until(restrict) - expected) < timedelta(minutes=2)

    (notice,) = tg.session.sent("SendMessage")
    assert notice["chat_id"] == GROUP_ID
    # The notice is cleaned up afterwards: the spam plus the notice were deleted.
    assert len(tg.session.sent("DeleteMessage")) == 2

    (event,) = await ge.recent_events()
    assert (event.kind, event.action, event.enforced) == ("hard", "mute", True)


async def test_hard_second_offence_bans_and_revokes_messages(tg):
    await ge.set_mode("enforce")
    await tg.feed(group_update(SPAM, message_id=1))
    await tg.feed(group_update(SPAM, message_id=2))
    await _drain()

    (ban,) = tg.session.sent("BanChatMember")
    assert ban["user_id"] == 777
    assert ban["revoke_messages"] is True
    assert [e.action for e in await ge.recent_events()] == ["ban", "mute"]


async def test_soft_ladder_warns_then_mutes_then_bans(tg):
    await ge.set_mode("enforce")

    await tg.feed(group_update(SOFT, message_id=1))
    assert len(tg.session.sent("DeleteMessage")) >= 1
    assert tg.session.sent("SendMessage") != []
    assert tg.session.sent("RestrictChatMember") == []

    await tg.feed(group_update(SOFT, message_id=2))
    assert len(tg.session.sent("RestrictChatMember")) == 1
    assert tg.session.sent("BanChatMember") == []

    await tg.feed(group_update(SOFT, message_id=3))
    assert len(tg.session.sent("BanChatMember")) == 1
    await _drain()


async def test_log_mode_only_records(tg):
    await ge.set_mode("log")
    await tg.feed(group_update(SPAM))

    names = [name for name, _ in tg.session.calls]
    assert names == ["GetChatMember"]
    (event,) = await ge.recent_events()
    assert (event.action, event.enforced) == ("mute", False)

    # Log-only events are not strikes: the first enforced offence is still a mute.
    await ge.set_mode("enforce")
    await tg.feed(group_update(SPAM, message_id=2))
    await _drain()
    assert len(tg.session.sent("RestrictChatMember")) == 1
    assert tg.session.sent("BanChatMember") == []


async def test_off_mode_does_nothing(tg):
    await ge.set_mode("off")
    await tg.feed(group_update(SPAM))
    assert tg.session.calls == []
    assert await ge.recent_events() == []


async def test_clean_message_makes_no_api_calls(tg):
    await ge.set_mode("enforce")
    await tg.feed(group_update("Бишкекке жолго чыгам 0555123456"))
    assert tg.session.calls == []


async def test_group_admins_are_exempt(tg):
    await ge.set_mode("enforce")
    tg.session.admin_ids = {777}
    await tg.feed(group_update(SPAM))
    assert tg.session.sent("DeleteMessage") == []
    assert tg.session.sent("RestrictChatMember") == []
    assert await ge.recent_events() == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"is_bot": True},
        {"sender_chat_id": -100555},
        {"user_id": 123456},  # the bot itself (token 123456:...)
    ],
)
async def test_bot_and_channel_senders_are_ignored(tg, kwargs):
    await ge.set_mode("enforce")
    await tg.feed(group_update(SPAM, **kwargs))
    assert tg.session.calls == []


async def test_caption_only_spam_is_deleted(tg):
    await ge.set_mode("enforce")
    await tg.feed(group_update(None, caption=SPAM, message_id=7))
    await _drain()
    assert {"chat_id": GROUP_ID, "message_id": 7} in tg.session.sent("DeleteMessage")


async def test_clean_message_edited_into_spam_is_handled(tg):
    await ge.set_mode("enforce")
    await tg.feed(group_update("Бишкек Ош", message_id=9))
    assert tg.session.calls == []
    await tg.feed(group_update(SPAM, message_id=9, edited=True))
    await _drain()
    assert {"chat_id": GROUP_ID, "message_id": 9} in tg.session.sent("DeleteMessage")


async def test_other_chats_are_ignored(tg):
    await ge.set_mode("enforce")
    await tg.feed(group_update(SPAM, chat_id=-100999))
    assert tg.session.calls == []


async def test_group_id_configured_as_username_still_matches(tg, monkeypatch):
    monkeypatch.setattr(config, "group_id", "@taxigroup")
    await ge.set_mode("enforce")
    await tg.feed(group_update(SPAM, chat_username="TaxiGroup"))
    await _drain()
    assert len(tg.session.sent("RestrictChatMember")) == 1


async def test_failing_delete_does_not_stop_the_rest(tg):
    await ge.set_mode("enforce")
    tg.session.fail["DeleteMessage"] = TelegramBadRequest(
        method=DeleteMessage(chat_id=1, message_id=1), message="message can't be deleted"
    )
    await tg.feed(group_update(SPAM))
    await _drain()
    assert len(tg.session.sent("RestrictChatMember")) == 1
    assert len(await ge.recent_events()) == 1


async def test_failing_admin_check_takes_no_action(tg):
    await ge.set_mode("enforce")
    tg.session.fail["GetChatMember"] = TelegramBadRequest(
        method=DeleteMessage(chat_id=1, message_id=1), message="user not found"
    )
    await tg.feed(group_update(SPAM))
    assert tg.session.sent("DeleteMessage") == []
    assert await ge.recent_events() == []


async def test_keyboard_removal_is_sent_once(tg):
    await gm.ensure_group_keyboard_removed(tg.bot)
    await gm.ensure_group_keyboard_removed(tg.bot)
    await _drain()
    (sent,) = tg.session.sent("SendMessage")
    assert sent["chat_id"] == GROUP_ID
    assert sent["reply_markup"] == {"remove_keyboard": True}


async def test_keyboard_removal_retries_after_a_failed_send(tg):
    tg.session.fail["SendMessage"] = TelegramBadRequest(
        method=DeleteMessage(chat_id=1, message_id=1), message="chat not found"
    )
    await gm.ensure_group_keyboard_removed(tg.bot)
    del tg.session.fail["SendMessage"]
    await gm.ensure_group_keyboard_removed(tg.bot)
    await _drain()
    assert len(tg.session.sent("SendMessage")) == 2  # failed attempt + successful retry


async def test_simultaneous_spam_escalates_instead_of_repeating_the_first_action(tg):
    await ge.set_mode("enforce")
    await asyncio.gather(
        tg.feed(group_update(SPAM, message_id=1)),
        tg.feed(group_update(SPAM, message_id=2)),
    )
    await _drain()

    assert len(tg.session.sent("RestrictChatMember")) == 1
    assert len(tg.session.sent("BanChatMember")) == 1
    assert sorted(e.action for e in await ge.recent_events()) == ["ban", "mute"]


async def test_link_hidden_behind_text_link_entity_is_deleted(tg):
    await ge.set_mode("enforce")
    await tg.feed(group_update("Нажми сюда", message_id=5, text_links=["https://scam.xyz"]))
    await _drain()
    assert {"chat_id": GROUP_ID, "message_id": 5} in tg.session.sent("DeleteMessage")
    assert tg.session.sent("RestrictChatMember") == []  # soft: first offence is a warning
