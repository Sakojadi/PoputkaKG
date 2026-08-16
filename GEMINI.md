# PoputkaKG Project Rules & Best Practices

## Tech Stack
- **Framework:** `aiogram` v3 (async Telegram bot framework)
- **Database:** `SQLAlchemy` (async) with `aiosqlite` (local) and `asyncpg` (production ready)
- **Background Tasks:** `APScheduler` (with `SQLAlchemyJobStore`)
- **Config Management:** `pydantic-settings`
- **Package Manager:** `uv`

## Architectural Guidelines
1. **Async Everywhere:** Never use synchronous blocking calls (like `requests` or `time.sleep`). Always use `aiohttp`, `asyncio.sleep`, and async session database calls.
2. **Database Connection:** Use `AsyncSessionLocal` context managers `async with AsyncSessionLocal() as session:` for all database transactions to avoid connection leaks.
3. **Database Locking & Schedulers:** SQLite strictly locks the database on writes. Never schedule an APScheduler persistent job *inside* an active uncommitted SQLAlchemy transaction. Always `await session.commit()` to release the SQLite lock before calling `scheduler.add_job()`.
4. **Locales:** Keep all strings in `bot/locales/translations.py`. Do not hardcode raw strings in handlers.
5. **Config & Environment:** Parse variables leniently in `bot/config.py` (using Pydantic `field_validators` for messy `.env` formats like comma-separated lists) to prevent crash loops for non-technical users.

## Deployment Checklist (Railway)
1. Ensure `.env` is securely loaded.
2. The root contains `railway.toml` with `startCommand = "uv run python -m bot.main"`.
3. Switch `DATABASE_URL` to a PostgreSQL instance on Railway for concurrent writing (SQLite is for local vibe coding and testing).
