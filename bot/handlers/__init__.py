from aiogram import Router

from .ad_flow import router as ad_flow_router
from .admin import router as admin_router
from .start import router as start_router


def get_handlers_router() -> Router:
    router = Router()
    router.include_router(start_router)
    router.include_router(ad_flow_router)
    router.include_router(admin_router)
    return router
