"""Moderation of every message posted in the configured group.

Flow per message: skip the bot / other bots / channel senders -> mode -> classify
-> (only when flagged) ask Telegram whether the sender is a group admin ->
decide the next rung of the ladder -> act, or just record it in log mode.
"""

import asyncio
import logging
import weakref
from datetime import UTC, datetime, timedelta

from aiogram import Bot, Router
from aiogram.types import Chat, ChatPermissions, Message, ReplyKeyboardRemove

from bot.config import config
from bot.handlers.ad_flow import _spawn
from bot.services.group_enforcement import (
    MUTE_HOURS,
    Action,
    get_mode,
    next_action,
    record_event,
)
from bot.services.group_filter import classify
from bot.services.settings_store import get_setting, set_setting

logger = logging.getLogger(__name__)

NOTICE_TTL_SECONDS = 15
KEYBOARD_FLAG = "group_keyboard_removed"

NOTICES = {
    Action.WARN: (
        "⚠️ {user}, билдирүүңүз группанын эрежесин бузгандыктан өчүрүлдү.\n"
        "⚠️ {user}, сообщение удалено: оно нарушает правила группы."
    ),
    Action.MUTE: (
        "🔇 {user} эрежени бузгандыктан 24 саатка жазуудан чектелди.\n"
        "🔇 {user} ограничен на 24 часа за нарушение правил."
    ),
    Action.BAN: (
        "🚫 {user} спам үчүн бөгөттөлдү.\n"
        "🚫 {user} заблокирован за спам."
    ),
}
KEYBOARD_NOTICE = (
    "ℹ️ Бот эми жеке жазышууда гана иштейт.\n"
    "ℹ️ Бот теперь работает только в личных сообщениях."
)

MUTED = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

router = Router(name="group_moderation")

# Updates are handled concurrently, so a burst of spam from one user would all
# read "no strikes yet" before the first was recorded. Decide-and-record is
# serialised per user; entries vanish once no handler holds the lock.
_user_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()


def _user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = _user_locks[user_id] = asyncio.Lock()
    return lock


def is_target_group(chat: Chat) -> bool:
    group_id = config.group_id
    if not group_id:
        return False
    if isinstance(group_id, int):
        return chat.id == group_id
    return bool(chat.username) and group_id.lower() == f"@{chat.username}".lower()


def _in_target_group(message: Message) -> bool:
    return message.chat.type in ("group", "supergroup") and is_target_group(message.chat)


router.message.filter(_in_target_group)
router.edited_message.filter(_in_target_group)


def _buttons(message: Message) -> list[tuple[str, str | None]]:
    markup = message.reply_markup
    if markup is None:
        return []
    return [(b.text, b.url) for row in markup.inline_keyboard for b in row]


def _entity_urls(message: Message) -> list[str]:
    """Targets of hyperlinked text ("click [here](url)"): the visible text hides them."""
    entities = [*(message.entities or []), *(message.caption_entities or [])]
    return [e.url for e in entities if e.type == "text_link" and e.url]


async def _is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool | None:
    """True/False, or None when Telegram could not tell us (then we do nothing)."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        logger.warning(f"Could not check admin status of {user_id} in {chat_id}: {e}")
        return None
    return member.status in ("creator", "administrator")


async def _try(label: str, awaitable) -> bool:
    """Run one Bot API call; a missing right or an already-gone message must
    never stop the remaining steps."""
    try:
        await awaitable
        return True
    except Exception as e:
        logger.warning(f"Group moderation: {label} failed: {e}")
        return False


async def _delete_later(bot: Bot, chat_id: int, message_id: int) -> None:
    await asyncio.sleep(NOTICE_TTL_SECONDS)
    await _try("delete notice", bot.delete_message(chat_id, message_id))


@router.message()
@router.edited_message()
async def moderate_group_message(message: Message, bot: Bot) -> None:
    user = message.from_user
    if (
        user is None
        or user.is_bot
        or user.id == bot.id
        or message.sender_chat is not None
    ):
        return

    mode = await get_mode()
    if mode == "off":
        return

    text = message.text or message.caption
    buttons = _buttons(message)
    verdict = classify(
        text,
        forwarded=message.forward_origin is not None,
        buttons=buttons,
        urls=_entity_urls(message),
    )
    if verdict is None:
        return

    # Asked only now, so clean messages cost no extra Bot API call.
    if await _is_group_admin(bot, message.chat.id, user.id) is not False:
        return

    snippet = text or " | ".join(label for label, _ in buttons) or verdict.matched
    chat_id = message.chat.id
    enforce = mode == "enforce"

    # Recorded before the slow Bot API calls so a concurrent message from the
    # same user already sees this strike.
    async with _user_lock(user.id):
        action = await next_action(user.id, verdict.kind)
        await record_event(
            user_id=user.id,
            chat_id=chat_id,
            message_id=message.message_id,
            kind=verdict.kind,
            rule=verdict.rule,
            action=action,
            enforced=enforce,
            snippet=snippet,
        )

    if not enforce:
        return

    await _try("delete message", bot.delete_message(chat_id, message.message_id))
    if action == Action.MUTE:
        until = datetime.now(UTC) + timedelta(hours=MUTE_HOURS)
        await _try(
            "restrict user",
            bot.restrict_chat_member(chat_id, user.id, MUTED, until_date=until),
        )
    elif action == Action.BAN:
        await _try("ban user", bot.ban_chat_member(chat_id, user.id, revoke_messages=True))
    try:
        notice = await bot.send_message(
            chat_id,
            NOTICES[action].format(user=user.mention_html()),
            parse_mode="HTML",
        )
        _spawn(_delete_later(bot, chat_id, notice.message_id))
    except Exception as e:
        logger.warning(f"Group moderation: could not send notice: {e}")


async def ensure_group_keyboard_removed(bot: Bot) -> None:
    """Once: take the stale reply keyboard (sent to the whole group by an old
    /start) off every member's screen. The flag is set only on success."""
    if not config.group_id or await get_setting(KEYBOARD_FLAG):
        return
    try:
        notice = await bot.send_message(
            config.group_id, KEYBOARD_NOTICE, reply_markup=ReplyKeyboardRemove()
        )
    except Exception as e:
        logger.warning(f"Could not remove the group reply keyboard: {e}")
        return
    await set_setting(KEYBOARD_FLAG, "1")
    _spawn(_delete_later(bot, config.group_id, notice.message_id))
