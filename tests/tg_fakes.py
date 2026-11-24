"""A fake Telegram Bot API for driving the real dispatcher in tests.

`FakeSession` records every Bot API call the bot makes and answers it locally;
`private_update` / `group_update` build the incoming updates.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import count

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetChatMember, SendMessage
from aiogram.types import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    MessageOriginHiddenUser,
    Update,
)
from aiogram.types import User as TgUser

GROUP_ID = -1001234567890
BOT_TOKEN = "123456:TEST-TOKEN-NOT-REAL"

_update_ids = count(1)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        # Users that GetChatMember reports as administrators.
        self.admin_ids: set[int] = set()
        # Method class name -> exception to raise instead of answering.
        self.fail: dict[str, Exception] = {}
        self._message_ids = count(5000)

    def sent(self, name: str) -> list[dict]:
        return [params for method, params in self.calls if method == name]

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.calls.append((name, method.model_dump(exclude_none=True)))
        if name in self.fail:
            raise self.fail[name]
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(self._message_ids),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="supergroup"),
                text=method.text,
            )
        if isinstance(method, GetChatMember):
            user = TgUser(id=method.user_id, is_bot=False, first_name="U")
            if method.user_id in self.admin_ids:
                return ChatMemberAdministrator.model_construct(
                    status="administrator", user=user
                )
            return ChatMemberMember(status="member", user=user)
        return True


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot(BOT_TOKEN, session=session), session


def private_update(text: str, user_id: int = 777) -> Update:
    message = Message(
        message_id=next(_update_ids),
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="U"),
        text=text,
    )
    return Update(update_id=next(_update_ids), message=message)


def group_update(
    text: str | None = None,
    *,
    user_id: int = 777,
    message_id: int = 1,
    caption: str | None = None,
    forward: bool = False,
    buttons: Sequence[tuple[str, str | None]] = (),
    is_bot: bool = False,
    sender_chat_id: int | None = None,
    edited: bool = False,
    chat_id: int = GROUP_ID,
    chat_username: str | None = None,
    text_links: Sequence[str] = (),
) -> Update:
    fields: dict = {
        "message_id": message_id,
        "date": datetime.now(UTC),
        "chat": Chat(id=chat_id, type="supergroup", title="Taxi", username=chat_username),
        "from_user": TgUser(id=user_id, is_bot=is_bot, first_name="U"),
        "text": text,
        "caption": caption,
    }
    if sender_chat_id is not None:
        fields["sender_chat"] = Chat(id=sender_chat_id, type="channel")
    if forward:
        fields["forward_origin"] = MessageOriginHiddenUser(
            type="hidden_user", date=datetime.now(UTC), sender_user_name="Spammer"
        )
    if text_links:
        length = len(text or caption or "x")
        fields["entities" if text else "caption_entities"] = [
            MessageEntity(type="text_link", offset=0, length=length, url=url)
            for url in text_links
        ]
    if buttons:
        fields["reply_markup"] = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=label, url=url)
                    if url
                    else InlineKeyboardButton(text=label, callback_data="x")
                    for label, url in buttons
                ]
            ]
        )
    message = Message(**fields)
    key = "edited_message" if edited else "message"
    return Update(update_id=next(_update_ids), **{key: message})
