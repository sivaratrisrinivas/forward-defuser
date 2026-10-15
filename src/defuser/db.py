"""Postgres access: a shared asyncpg pool and a tiny forward-only migration runner."""

import asyncio
import json
from pathlib import Path

import asyncpg

from defuser.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def get_pool(dsn: str | None = None) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn or get_settings().database_url, min_size=1, max_size=10, init=_init_conn
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def vec(values) -> str:
    """pgvector text literal."""
    return "[" + ",".join(f"{float(v):.6f}" for v in values) + "]"


async def migrate(dsn: str | None = None) -> list[str]:
    conn = await asyncpg.connect(dsn or get_settings().database_url)
    applied: list[str] = []
    try:
        # Serialize concurrent deploys.
        await conn.execute("SELECT pg_advisory_lock(727274)")
        exists = await conn.fetchval("SELECT to_regclass('schema_migrations') IS NOT NULL")
        done = set()
        if exists:
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations(version) VALUES ($1)", path.stem)
            applied.append(path.stem)
    finally:
        await conn.execute("SELECT pg_advisory_unlock(727274)")
        await conn.close()
    return applied


def migrate_main() -> None:
    applied = asyncio.run(migrate())
    print("applied:", ", ".join(applied) or "nothing")
