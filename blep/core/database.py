"""Thin asyncpg wrapper with JSON codecs and file-based migrations."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"

# Arbitrary constant used to serialise concurrent migrations (multiple shards/processes).
_MIGRATION_LOCK = 0xB1E9


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Database:
    """Connection pool helper.

    Every method mirrors the asyncpg pool method of the same name, so cogs can write
    ``await self.bot.db.fetchrow("SELECT ...", guild_id)``.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 2, max_size: int = 20) -> Database:
        pool = await asyncpg.create_pool(
            dsn,
            init=_init_connection,
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
        )
        assert pool is not None
        return cls(pool)

    async def migrate(self) -> None:
        """Run every ``blep/schema/*.sql`` file in filename order.

        Schema files must be idempotent (``CREATE TABLE IF NOT EXISTS`` /
        ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``) because they run on every boot.
        """
        files = sorted(SCHEMA_DIR.glob("*.sql"))
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", _MIGRATION_LOCK)
                for path in files:
                    log.debug("Applying schema %s", path.name)
                    await conn.execute(path.read_text(encoding="utf-8"))
        log.info("Applied %d schema files", len(files))

    async def close(self) -> None:
        await self.pool.close()

    # Pass-through helpers -------------------------------------------------- #
    async def execute(self, query: str, *args: Any, timeout: float | None = None) -> str:
        return await self.pool.execute(query, *args, timeout=timeout)

    async def executemany(self, query: str, args: Any, *, timeout: float | None = None) -> None:
        await self.pool.executemany(query, args, timeout=timeout)

    async def fetch(self, query: str, *args: Any, timeout: float | None = None) -> list[asyncpg.Record]:
        return await self.pool.fetch(query, *args, timeout=timeout)

    async def fetchrow(self, query: str, *args: Any, timeout: float | None = None) -> asyncpg.Record | None:
        return await self.pool.fetchrow(query, *args, timeout=timeout)

    async def fetchval(self, query: str, *args: Any, column: int = 0, timeout: float | None = None) -> Any:
        return await self.pool.fetchval(query, *args, column=column, timeout=timeout)

    def acquire(self):  # noqa: ANN201 - asyncpg's PoolAcquireContext
        return self.pool.acquire()
