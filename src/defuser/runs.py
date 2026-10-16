"""Persistence of runs, evidence and review items."""

import hashlib

import asyncpg

from defuser.checker import CheckResult


async def start_run(
    pool: asyncpg.Pool, run_id: str, inbound_id: int | None, purpose: str, claim_id: int | None = None
) -> None:
    await pool.execute(
        """INSERT INTO runs (id, inbound_id, claim_id, purpose) VALUES ($1, $2, $3, $4)
           ON CONFLICT (id) DO NOTHING""",
        run_id,
        inbound_id,
        claim_id,
        purpose,
    )


async def finish_run(
    pool: asyncpg.Pool,
    res: CheckResult,
    cache_hit: str = "none",
    claim_id: int | None = None,
    status: str = "done",
) -> None:
    m = res.meter
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """UPDATE runs SET status = $2, verdict = $3, confidence = $4, lang = $5, cache_hit = $6, steps = $7,
                   input_tokens = $8, output_tokens = $9, cost_usd = $10, latency_ms = $11, verifier_passed = $12,
                   verifier_errors = $13, trace = $14, finished_at = now(), claim_id = COALESCE($15, claim_id)
               WHERE id = $1""",
            res.run_id,
            status,
            res.verdict.value,
            res.confidence,
            res.lang,
            cache_hit,
            m.steps if m else 0,
            m.input_tokens if m else 0,
            m.output_tokens if m else 0,
            round(m.cost, 5) if m else 0,
            res.latency_ms,
            res.report.passed if res.report else None,
            [e.model_dump() for e in res.report.errors] if res.report else [],
            res.trace(),
            claim_id,
        )
        for ev in res.evidence.values():
            await conn.execute(
                """INSERT INTO evidence (run_id, evidence_id, url, final_url, title, publisher, tier, label, text, text_sha256)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (run_id, evidence_id) DO NOTHING""",
                res.run_id,
                ev.evidence_id,
                ev.url,
                ev.final_url,
                ev.title,
                ev.publisher,
                ev.tier.value,
                ev.label.value if ev.label else None,
                ev.text,
                hashlib.sha256(ev.text.encode()).hexdigest(),
            )
        if res.needs_review:
            await conn.execute(
                """INSERT INTO review_items (run_id, claim_id, reason) VALUES ($1, $2, $3) ON CONFLICT (run_id) DO NOTHING""",
                res.run_id,
                claim_id,
                res.needs_review,
            )
