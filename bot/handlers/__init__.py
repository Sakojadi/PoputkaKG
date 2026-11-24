from aiogram import F, Router

from .ad_flow import router as ad_flow_router
from .group_moderation import router as group_moderation_router
from .start import router as start_router


def get_handlers_router() -> Router:
    # Everything the bot says in reply to a user belongs in a private chat.
    # Without this, a /start typed in the group made the bot post its welcome
    # message and reply keyboard there, and the ad flow then ran in the group.
    private = Router(name="private")
    private.message.filter(F.chat.type == "private")
    private.callback_query.filter(F.message.chat.type == "private")
    private.include_router(start_router)
    private.include_router(ad_flow_router)

    router = Router()
    router.include_router(private)
    router.include_router(group_moderation_router)
    return router
