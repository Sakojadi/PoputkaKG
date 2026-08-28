"""Wipe every row from the database and restart id sequences.

Works against whatever DATABASE_URL points at, so the same script clears the
local sqlite file and the Railway Postgres. Destructive and irreversible.

Run:  uv run python -m scripts.wipe_db          # dry run, prints row counts
      uv run python -m scripts.wipe_db --yes    # actually wipes

Stop the bot before wiping. A running process holds APScheduler jobs in memory
and will write them straight back into an emptied jobstore.
"""

import asyncio
import sys

from sqlalchemy import text

from bot.database.db import engine

# apscheduler_jobs is not a SQLAlchemy model of ours -- it is APScheduler's own
# jobstore table, living in the same database. Clearing campaigns without it
# leaves orphan jobs that fire for campaigns that no longer exist.
TABLES = [
    "payments",
    "campaigns",
    "group_posts",
    "users",
    "app_settings",
    "apscheduler_jobs",
]


def _redacted_url() -> str:
    """The DB target, with any password removed, safe to print."""
    url = engine.url
    return str(url.render_as_string(hide_password=True))


async def _count(conn, table: str) -> int | None:
    """Row count, or None when the table does not exist here."""
    try:
        result = await conn.execute(text(f'SELECT count(*) FROM "{table}"'))
        return int(result.scalar() or 0)
    except Exception:
        return None


async def main() -> None:
    confirmed = "--yes" in sys.argv
    is_postgres = engine.dialect.name == "postgresql"

    print(f"target:  {_redacted_url()}")
    print(f"dialect: {engine.dialect.name}\n")

    async with engine.connect() as conn:
        counts = {t: await _count(conn, t) for t in TABLES}

    total = 0
    for table, count in counts.items():
        if count is None:
            print(f"  {table:20} (нет такой таблицы, пропускаю)")
        else:
            print(f"  {table:20} {count}")
            total += count

    if not confirmed:
        print(f"\nDRY RUN. Будет удалено {total} строк.")
        print("Запустите с --yes, чтобы удалить на самом деле.")
        return

    present = [t for t, c in counts.items() if c is not None]

    async with engine.begin() as conn:
        if is_postgres:
            # One statement: TRUNCATE takes all the locks at once and RESTART
            # IDENTITY resets the id sequences so the next campaign is id 1.
            quoted = ", ".join(f'"{t}"' for t in present)
            await conn.execute(
                text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")
            )
        else:
            for table in present:
                await conn.execute(text(f'DELETE FROM "{table}"'))
            # sqlite keeps autoincrement counters in a side table; without this
            # the next campaign would carry on from the old max id.
            try:
                await conn.execute(text("DELETE FROM sqlite_sequence"))
            except Exception:
                # No AUTOINCREMENT column has ever been used in this file.
                pass

    async with engine.connect() as conn:
        # A plain loop, not sum(await ... for ...): that builds an async
        # generator, which sum() cannot consume.
        left = 0
        for table in present:
            left += await _count(conn, table) or 0

    await engine.dispose()
    print(f"\nУдалено {total} строк. Осталось: {left}.")
    if left:
        print("ВНИМАНИЕ: база не пустая, проверьте вручную.")


if __name__ == "__main__":
    asyncio.run(main())
