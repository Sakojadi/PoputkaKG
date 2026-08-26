"""Single shared Bot instance for the whole process.

Creating a Bot() is expensive: aiogram builds a fresh SSL context (loading the
full certifi CA bundle) inside the constructor. Doing that per scheduled job or
per web request leaks memory faster than the GC returns it to the OS.
"""

from aiogram import Bot

from bot.config import config

_bot: Bot | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=config.bot_token)
    return _bot


async def close_bot() -> None:
    global _bot
    if _bot is not None:
        await _bot.session.close()
        _bot = None
