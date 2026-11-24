from datetime import datetime, timedelta

import pytest

from bot.database.db import AsyncSessionLocal
from bot.database.models import ModerationEvent
from bot.services import group_enforcement as ge
from bot.services.group_enforcement import Action

NOW = datetime(2026, 11, 21, 12, 0, 0)  # noqa: DTZ001 - the DB stores naive UTC


async def _strike(kind, *, user_id=1, enforced=True, age=timedelta(0)):
    async with AsyncSessionLocal() as session:
        session.add(
            ModerationEvent(
                user_id=user_id,
                chat_id=-100,
                message_id=1,
                kind=kind,
                rule="r",
                action="warn",
                enforced=enforced,
                snippet="s",
                created_at=NOW - age,
            )
        )
        await session.commit()


async def test_hard_ladder_is_mute_then_ban(db):
    assert await ge.next_action(1, "hard", NOW) == Action.MUTE
    await _strike("hard")
    assert await ge.next_action(1, "hard", NOW) == Action.BAN


async def test_log_only_events_are_not_strikes(db):
    await _strike("hard", enforced=False)
    assert await ge.next_action(1, "hard", NOW) == Action.MUTE


async def test_hard_strikes_never_expire(db):
    await _strike("hard", age=timedelta(days=30))
    assert await ge.next_action(1, "hard", NOW) == Action.BAN


async def test_soft_ladder_is_warn_mute_ban(db):
    assert await ge.next_action(1, "soft", NOW) == Action.WARN
    await _strike("soft")
    assert await ge.next_action(1, "soft", NOW) == Action.MUTE
    await _strike("soft", age=timedelta(hours=1))
    assert await ge.next_action(1, "soft", NOW) == Action.BAN


async def test_soft_strikes_expire_after_24_hours(db):
    await _strike("soft", age=timedelta(hours=25))
    assert await ge.next_action(1, "soft", NOW) == Action.WARN


async def test_hard_and_soft_counters_are_independent(db):
    await _strike("hard")
    assert await ge.next_action(1, "soft", NOW) == Action.WARN
    await _strike("soft", user_id=2)
    assert await ge.next_action(1, "hard", NOW) == Action.BAN
    assert await ge.next_action(3, "hard", NOW) == Action.MUTE


async def test_mode_defaults_to_log(db):
    assert await ge.get_mode() == "log"


async def test_invalid_stored_mode_falls_back_to_log(db):
    from bot.services.settings_store import set_setting

    await set_setting("group_filter_mode", "bogus")
    assert await ge.get_mode() == "log"


async def test_mode_round_trips_and_rejects_unknown(db):
    await ge.set_mode("enforce")
    assert await ge.get_mode() == "enforce"
    with pytest.raises(ValueError):
        await ge.set_mode("x")


async def test_recent_events_are_newest_first_and_snippet_is_truncated(db):
    for i in range(3):
        await ge.record_event(
            user_id=1,
            chat_id=-100,
            message_id=i,
            kind="soft",
            rule="soft_word",
            action=Action.WARN,
            enforced=True,
            snippet="x" * 500,
        )
    events = await ge.recent_events(limit=2)
    assert [e.message_id for e in events] == [2, 1]
    assert len(events[0].snippet) == 200
