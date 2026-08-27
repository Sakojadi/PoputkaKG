import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

XPAY_BASE_URLS = {
    "sandbox": "https://devapi.xpay.kg",
    "production": "https://api.xpay.kg",
}

# "mock" takes no money and reports every payment as paid; "xpay" is the real
# integration, whose sandbox/production target is chosen by XPAY_MODE.
PAYMENT_PROVIDERS = ("mock", "xpay")


class Settings(BaseSettings):
    bot_token: str
    group_id: int | str = ""
    payment_provider: str = "mock"
    xpay_client_id: str = ""
    xpay_client_secret: str = ""
    xpay_mode: str = "sandbox"
    public_base_url: str = ""
    openai_api_key: str = ""
    database_url: str = "sqlite+aiosqlite:///bot.db"
    admin_ids: list[int] = []

    # Web Admin Dashboard Settings
    # No defaults: a hardcoded secret/password committed to the repo would let
    # anyone forge an admin session against any deployment that forgot to
    # override it. Missing either must crash at startup, not silently work.
    admin_username: str = "admin"
    admin_password: str
    secret_key: str
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

    @field_validator("payment_provider", mode="before")
    @classmethod
    def parse_payment_provider(cls, v):
        val = str(v or "mock").strip().strip("'\"").lower()
        if val not in PAYMENT_PROVIDERS:
            raise ValueError(
                f"PAYMENT_PROVIDER must be one of {list(PAYMENT_PROVIDERS)}, "
                f"got {val!r}"
            )
        return val

    @field_validator("xpay_mode", mode="before")
    @classmethod
    def parse_xpay_mode(cls, v):
        val = str(v or "sandbox").strip().strip("'\"").lower()
        if val not in XPAY_BASE_URLS:
            raise ValueError(
                f"XPAY_MODE must be one of {sorted(XPAY_BASE_URLS)}, got {val!r}"
            )
        return val

    @property
    def xpay_base_url(self) -> str:
        return XPAY_BASE_URLS[self.xpay_mode]

    @property
    def resolved_public_base_url(self) -> str | None:
        """Origin xPay should call back to, or None when we are not reachable.

        None means local development: the caller omits callback_url entirely
        and the flow falls back to the user's "Check payment" button.
        """
        val = self.public_base_url.strip().strip("'\"").rstrip("/")
        if val:
            return val
        domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().strip("'\"")
        return f"https://{domain}" if domain else None

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


config = Settings()
