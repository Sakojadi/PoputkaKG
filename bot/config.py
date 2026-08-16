from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    bot_token: str
    group_id: int | str = ""
    xpay_api_key: str = ""
    database_url: str = "sqlite+aiosqlite:///bot.db"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

config = Settings()
