from aiogram import Router
from .start import router as start_router
from .ad_flow import router as ad_flow_router

def get_handlers_router() -> Router:
    router = Router()
    router.include_router(start_router)
    router.include_router(ad_flow_router)
    return router
