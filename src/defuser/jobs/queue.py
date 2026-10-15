"""Durable Postgres job queue.

- enqueue() is idempotent on `idempotency_key` (duplicate webhooks, retried schedulers, restarts).
- claim() leases a job with FOR UPDATE SKIP LOCKED. A worker that dies stops renewing its lease, and the job
  becomes claimable again once `locked_until` passes.
- Failures back off exponentially; after `max_attempts` a job is marked `dead` and shows on the dashboard.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    idempotency_key: str
    attempts: int
    max_attempts: int


async def enqueue(
    conn: asyncpg.Connection | asyncpg.Pool,
    kind: str,
    payload: dict,
    idempotency_key: str,
    run_at: datetime | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Returns the new job id, or None when a job with this key already exists."""
    return await conn.fetchval(
        """
        INSERT INTO jobs (kind, payload, idempotency_key, run_at, max_attempts)
        VALUES ($1, $2, $3, COALESCE($4, now()), $5)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        kind,
        payload,
        idempotency_key,
        run_at,
        max_attempts,
    )


async def claim(
    pool: asyncpg.Pool, worker_id: str, lease_s: int = 60, kinds: list[str] | None = None
) -> Job | None:
    row = await pool.fetchrow(
        """
        UPDATE jobs SET status = 'running', locked_by = $1, locked_until = now() + make_interval(secs => $2),
                        attempts = attempts + 1
        WHERE id = (
            SELECT id FROM jobs
            WHERE run_at <= now()
              AND (status = 'queued' OR (status = 'running' AND locked_until < now()))
              AND ($3::text[] IS NULL OR kind = ANY($3))
            ORDER BY run_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, kind, payload, idempotency_key, attempts, max_attempts
        """,
        worker_id,
        float(lease_s),
        kinds,
    )
    if row is None:
        return None
    return Job(**dict(row))


async def heartbeat(pool: asyncpg.Pool, job_id: int, worker_id: str, lease_s: int = 60) -> bool:
    res = await pool.execute(
        "UPDATE jobs SET locked_until = now() + make_interval(secs => $3) WHERE id = $1 AND locked_by = $2 AND status = 'running'",
        job_id,
        worker_id,
        float(lease_s),
    )
    return res.endswith("1")


async def complete(pool: asyncpg.Pool, job_id: int, worker_id: str) -> None:
    await pool.execute(
        "UPDATE jobs SET status = 'done', finished_at = now(), locked_by = NULL, locked_until = NULL "
        "WHERE id = $1 AND locked_by = $2",
        job_id,
        worker_id,
    )


async def fail(pool: asyncpg.Pool, job: Job, worker_id: str, error: str) -> None:
    dead = job.attempts >= job.max_attempts
    delay = timedelta(seconds=min(600, 5 * 2 ** (job.attempts - 1)))
    await pool.execute(
        """
        UPDATE jobs SET status = $3, last_error = $4, run_at = $5, locked_by = NULL, locked_until = NULL,
                        finished_at = CASE WHEN $3 = 'dead' THEN now() END
        WHERE id = $1 AND locked_by = $2
        """,
        job.id,
        worker_id,
        "dead" if dead else "queued",
        error[:2000],
        datetime.now(UTC) + delay,
    )
