import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    group_id: int | str = ""
    xpay_api_key: str = ""
    openai_api_key: str = ""
    database_url: str = "sqlite+aiosqlite:///bot.db"
    admin_ids: list[int] = []

    # Web Admin Dashboard Settings
    admin_username: str = "admin"
    admin_password: str = "admin12345"
    secret_key: str = "secret-super-key-poputka-admin-xyz-123"
    port: int = 8000

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if isinstance(v, str):
            v = v.strip("[]'\" ")
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("group_id", mode="before")
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

    @field_validator("database_url", mode="before")
    @classmethod
    def parse_db_url(cls, v):
        env_db = (
            os.getenv("DATABASE_URL")
            or os.getenv("DATABASE_PUBLIC_URL")
            or os.getenv("DATABASE_PRIVATE_URL")
            or os.getenv("POSTGRES_URL")
        )
        val = env_db or v
        if not val:
            return "sqlite+aiosqlite:///bot.db"
        val = str(val).strip().strip("'\"")
        if val.startswith("postgres://"):
            return val.replace("postgres://", "postgresql+asyncpg://", 1)
        if val.startswith("postgresql://") and not val.startswith(
            "postgresql+asyncpg://"
        ):
            return val.replace("postgresql://", "postgresql+asyncpg://", 1)
        return val

    @field_validator("port", mode="before")
    @classmethod
    def parse_port(cls, v):
        env_port = os.getenv("PORT")
        if env_port:
            try:
                return int(env_port)
            except ValueError:
                pass
        if v:
            try:
                return int(v)
            except ValueError:
                pass
        return 8000

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


config = Settings()
