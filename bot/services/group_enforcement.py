"""Escalation ladder, strike ledger and mode switch for group moderation."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select

from bot.database.db import AsyncSessionLocal
from bot.database.models import ModerationEvent
from bot.services.settings_store import get_setting, set_setting

MUTE_HOURS = 24
SOFT_WINDOW = timedelta(hours=24)
SNIPPET_MAX = 200

MODE_KEY = "group_filter_mode"
MODES = ("off", "log", "enforce")
DEFAULT_MODE = "log"


class Action(StrEnum):
    WARN = "warn"
    MUTE = "mute"
    BAN = "ban"


def _utc_now() -> datetime:
    """Naive UTC, matching how created_at is stored."""
    return datetime.now(UTC).replace(tzinfo=None)


async def next_action(user_id: int, kind: str, now: datetime | None = None) -> Action:
    """What the next enforced offence of `kind` by this user should trigger.

    Only enforced events count. Hard strikes never expire (a spammer returning
    after the mute is still on strike two); soft strikes count within 24 h.
    """
    now = now or _utc_now()
    query = (
        select(func.count())
        .select_from(ModerationEvent)
        .where(
            ModerationEvent.user_id == user_id,
            ModerationEvent.kind == kind,
            ModerationEvent.enforced.is_(True),
        )
    )
    if kind == "soft":
        query = query.where(ModerationEvent.created_at > now - SOFT_WINDOW)
    async with AsyncSessionLocal() as session:
        prior = (await session.execute(query)).scalar() or 0

    if kind == "hard":
        return Action.MUTE if prior == 0 else Action.BAN
    if prior == 0:
        return Action.WARN
    return Action.MUTE if prior == 1 else Action.BAN


async def record_event(
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
    kind: str,
    rule: str,
    action: Action,
    enforced: bool,
    snippet: str,
) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            ModerationEvent(
                user_id=user_id,
                chat_id=chat_id,
                message_id=message_id,
                kind=kind,
                rule=rule,
                action=str(action),
                enforced=enforced,
                snippet=(snippet or "")[:SNIPPET_MAX],
            )
        )
        await session.commit()


async def recent_events(limit: int = 100) -> list[ModerationEvent]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ModerationEvent).order_by(ModerationEvent.id.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def get_mode() -> str:
    value = await get_setting(MODE_KEY, DEFAULT_MODE)
    return value if value in MODES else DEFAULT_MODE


async def set_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    await set_setting(MODE_KEY, mode)
