from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import List, Union

class Settings(BaseSettings):
    bot_token: str
    group_id: Union[int, str] = ""
    xpay_api_key: str = ""
    openai_api_key: str = ""
    database_url: str = "sqlite+aiosqlite:///bot.db"
    admin_ids: List[int] = []

    @field_validator('admin_ids', mode='before')
    @classmethod
    def parse_admin_ids(cls, v):
        if isinstance(v, str):
            v = v.strip("[]'\" ")
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator('group_id', mode='before')
    @classmethod
    def parse_group_id(cls, v):
        if isinstance(v, str):
            v = v.strip().strip("'\"")
            try:
                return int(v)
            except ValueError:
                if not v.startswith("@"):
                    return f"@{v}"
                return v
        return v


    @field_validator('database_url', mode='before')
    @classmethod
    def parse_db_url(cls, v):
        if not v:
            return "sqlite+aiosqlite:///bot.db"
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        if v.startswith("postgresql://") and not v.startswith("postgresql+asyncpg://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

config = Settings()
