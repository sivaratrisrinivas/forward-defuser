"""Run journal: durable, ordered record of LLM responses and tool outcomes.

On a fresh run every step executes and is persisted. When a killed run is resumed, steps whose key matches the
journal are replayed from storage instead of re-executed, so the run reaches the same state without paying for
LLM calls twice or re-fetching pages. A key mismatch (code changed between attempts) truncates the journal at
that point and continues live.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable

import asyncpg


def step_key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


class Journal:
    def __init__(self, pool: asyncpg.Pool | None, run_id: str):
        self.pool, self.run_id = pool, run_id
        self.entries: list[dict] = []
        self.seq = 0
        self.replayed = 0

    async def load(self) -> "Journal":
        if self.pool is not None:
            rows = await self.pool.fetch(
                "SELECT seq, kind, key, payload FROM run_journal WHERE run_id = $1 ORDER BY seq", self.run_id
            )
            self.entries = [dict(r) for r in rows]
        return self

    async def step(self, kind: str, key: str, fn: Callable[[], Awaitable[dict]]) -> tuple[dict, bool]:
        """Returns (payload, replayed)."""
        if self.seq < len(self.entries):
            e = self.entries[self.seq]
            if e["kind"] == kind and e["key"] == key:
                self.seq += 1
                self.replayed += 1
                return e["payload"], True
            await self._truncate()
        payload = await fn()
        if self.pool is not None:
            await self.pool.execute(
                "INSERT INTO run_journal (run_id, seq, kind, key, payload) VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (run_id, seq) DO UPDATE SET kind = EXCLUDED.kind, key = EXCLUDED.key, payload = EXCLUDED.payload",
                self.run_id,
                self.seq,
                kind,
                key,
                payload,
            )
        self.entries = self.entries[: self.seq] + [
            {"seq": self.seq, "kind": kind, "key": key, "payload": payload}
        ]
        self.seq += 1
        return payload, False

    async def _truncate(self) -> None:
        if self.pool is not None:
            await self.pool.execute(
                "DELETE FROM run_journal WHERE run_id = $1 AND seq >= $2", self.run_id, self.seq
            )
        self.entries = self.entries[: self.seq]
