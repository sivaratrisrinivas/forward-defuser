"""Budgets and rate limits.

- per user per minute (spam): checked at ingest, Redis INCR with expiry
- per user per day: message count and LLM spend
- global per day: LLM spend
- per message: enforced inside the run by the Meter (steps and cost)

Redis is the fast path. If Redis is unreachable, daily spend falls back to summing today's runs in Postgres, so
an outage never means unlimited spend.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg
import redis.asyncio as aioredis

from defuser.config import get_settings
from defuser.observability import BUDGET_REJECTS

log = logging.getLogger(__name__)
_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url, socket_timeout=1.0, socket_connect_timeout=1.0)
    return _redis


def reset_redis() -> None:
    global _redis
    _redis = None


def _day() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


@dataclass
class Allowance:
    ok: bool
    reason: str = ""  # rate_limited | user_daily | global_daily
    max_cost: float = 0.0
    notify: bool = True  # send the "slow down" message (only once per window)


async def check_rate(user_hash: str) -> Allowance:
    s = get_settings()
    key = f"rl:{user_hash}:{int(datetime.now(UTC).timestamp() // 60)}"
    try:
        r = get_redis()
        n = await r.incr(key)
        if n == 1:
            await r.expire(key, 120)
    except Exception as e:  # noqa: BLE001
        log.warning("rate limiter unavailable: %s", e)
        return Allowance(ok=True)
    if n > s.max_messages_per_user_minute:
        BUDGET_REJECTS.labels("rate").inc()
        return Allowance(ok=False, reason="rate_limited", notify=n == s.max_messages_per_user_minute + 1)
    return Allowance(ok=True)


async def _spent_db(pool: asyncpg.Pool, user_hash: str | None) -> tuple[float, float, int]:
    row = await pool.fetchrow(
        """SELECT COALESCE(SUM(r.cost_usd), 0) AS global_cost,
                  COALESCE(SUM(r.cost_usd) FILTER (WHERE i.user_hash = $1), 0) AS user_cost,
                  COUNT(*) FILTER (WHERE i.user_hash = $1) AS user_msgs
           FROM runs r LEFT JOIN inbound_messages i ON i.id = r.inbound_id
           WHERE r.started_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""",
        user_hash,
    )
    return float(row["global_cost"]), float(row["user_cost"]), int(row["user_msgs"])


async def reserve(pool: asyncpg.Pool, user_hash: str) -> Allowance:
    """Called before a check. Counts the message and returns the cost allowance for this run."""
    s = get_settings()
    day = _day()
    try:
        r = get_redis()
        pipe = r.pipeline()
        pipe.incr(f"msgs:{user_hash}:{day}")
        pipe.expire(f"msgs:{user_hash}:{day}", 2 * 86400)
        pipe.get(f"cost:{user_hash}:{day}")
        pipe.get(f"cost:global:{day}")
        msgs, _, user_cost, global_cost = await pipe.execute()
        user_cost, global_cost = float(user_cost or 0), float(global_cost or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("redis unavailable, using database totals: %s", e)
        global_cost, user_cost, msgs = await _spent_db(pool, user_hash)
        msgs += 1
    if global_cost >= s.max_cost_global_day_usd:
        BUDGET_REJECTS.labels("global").inc()
        return Allowance(ok=False, reason="global_daily")
    if user_cost >= s.max_cost_per_user_day_usd or msgs > s.max_messages_per_user_day:
        BUDGET_REJECTS.labels("user").inc()
        return Allowance(ok=False, reason="user_daily")
    max_cost = min(
        s.max_cost_per_message_usd,
        s.max_cost_per_user_day_usd - user_cost,
        s.max_cost_global_day_usd - global_cost,
    )
    return Allowance(ok=True, max_cost=max(0.0, max_cost))


async def record_spend(user_hash: str, cost: float) -> None:
    if cost <= 0:
        return
    day = _day()
    try:
        r = get_redis()
        pipe = r.pipeline()
        for key in (f"cost:{user_hash}:{day}", f"cost:global:{day}"):
            pipe.incrbyfloat(key, cost)
            pipe.expire(key, 2 * 86400)
        await pipe.execute()
    except Exception as e:  # noqa: BLE001 - the runs table remains the source of truth
        log.warning("could not record spend in redis: %s", e)
