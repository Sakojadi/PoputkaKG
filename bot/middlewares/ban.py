from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from bot.database.db import AsyncSessionLocal
from bot.database.models import User

class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
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
                        await event.answer("❌ Сиздин аккаунтуңуз бөгөттөлгөн / Ваш аккаунт заблокирован.")
                    elif isinstance(event, CallbackQuery):
                        await event.answer("❌ Сиздин аккаунтуңуз бөгөттөлгөн / Ваш аккаунт заблокирован.", show_alert=True)
                    return
                    
        return await handler(event, data)
