from sqlalchemy import select

from bot.database.db import AsyncSessionLocal
from bot.database.models import AppSetting

DEFAULT_PRICE_PER_AD = 1.0

# The bot and the web panel run in the same process, so this in-memory cache
# stays coherent with the DB as long as every write goes through set_setting().
_cache: dict[str, str] = {}


async def load_settings() -> None:
    """Populate the in-process cache from the app_settings table. Call once at startup."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(AppSetting))
        _cache.clear()
        _cache.update({row.key: row.value for row in result.scalars().all()})


async def get_setting(key: str, default: str | None = None) -> str | None:
    if key in _cache:
        return _cache[key]
    async with AsyncSessionLocal() as session:
        setting = await session.get(AppSetting, key)
    if setting is None:
        return default
    _cache[key] = setting.value
    return setting.value


async def set_setting(key: str, value: str) -> None:
    await set_settings({key: value})


async def set_settings(values: dict[str, str]) -> None:
    """Write several keys in one transaction, so related settings (e.g. a
    password hash and its salt) can never be observed half-written."""
    async with AsyncSessionLocal() as session:
        for key, value in values.items():
            setting = await session.get(AppSetting, key)
            if setting is None:
                session.add(AppSetting(key=key, value=value))
            else:
                setting.value = value
        await session.commit()
    _cache.update(values)


async def get_price_per_ad() -> float:
    value = await get_setting("price_per_ad", str(DEFAULT_PRICE_PER_AD))
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_PRICE_PER_AD
