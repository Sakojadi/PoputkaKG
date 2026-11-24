from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.database.db import AsyncSessionLocal
from bot.database.models import User


class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Bans and user rows are about the private chat with the bot. In the
        # group this middleware would answer "account blocked" to banned users
        # and insert a row for every member who ever posted.
        chat = data.get("event_chat")
        if chat is not None and chat.type != "private":
            return await handler(event, data)

        user = data.get("event_from_user")
        if user:
            async with AsyncSessionLocal() as session:
                db_user = await session.get(User, user.id)
                if db_user:
                    if user.username and db_user.username != user.username:
                        db_user.username = user.username
                        await session.commit()
                else:
                    db_user = User(id=user.id, username=user.username, language="ky")
                    session.add(db_user)
                    await session.commit()

                if getattr(db_user, "is_banned", False):
                    # Reject all interactions from banned user
                    if isinstance(event, Message):
                        await event.answer(
                            "❌ Сиздин аккаунтуңуз бөгөттөлгөн / Ваш аккаунт заблокирован."
                        )
                    elif isinstance(event, CallbackQuery):
                        await event.answer(
                            "❌ Сиздин аккаунтуңуз бөгөттөлгөн / Ваш аккаунт заблокирован.",
                            show_alert=True,
                        )
                    return

        return await handler(event, data)
