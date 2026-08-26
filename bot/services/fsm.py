"""Access to the dispatcher's FSM storage from outside the polling loop.

bot/main.py gathers dp.start_polling() and the uvicorn server on a single
event loop in a single process, so the web layer can move a user's FSM
state directly. This is a shared reference, not IPC.

Storage is in-memory, so state does not survive a restart. The Payment row
is the durable record; see the spec's "Known limitations".
"""

import logging

from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from bot.services.tg import get_bot

logger = logging.getLogger(__name__)

_dispatcher: Dispatcher | None = None


def set_dispatcher(dp: Dispatcher) -> None:
    global _dispatcher
    _dispatcher = dp


def get_fsm_context(user_id: int) -> FSMContext | None:
    """FSM context for a user's private chat with the bot.

    Returns None when no dispatcher has been registered yet, which happens
    only in tests and during early startup.
    """
    if _dispatcher is None:
        logger.warning("FSM context requested before the dispatcher was registered")
        return None
    bot = get_bot()
    # The bot only ever talks to users in private chats, where chat_id
    # equals user_id.
    return FSMContext(
        storage=_dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id),
    )
