from aiogram import Dispatcher

from bot.handlers import get_handlers_router
from bot.middlewares import BanMiddleware


def build_dispatcher() -> Dispatcher:
    """The dispatcher with its middleware and handlers.

    Routers are module-level singletons, so this can only be called once per
    process.
    """
    dp = Dispatcher()

    ban_middleware = BanMiddleware()
    dp.message.outer_middleware(ban_middleware)
    dp.callback_query.outer_middleware(ban_middleware)

    dp.include_router(get_handlers_router())
    return dp
