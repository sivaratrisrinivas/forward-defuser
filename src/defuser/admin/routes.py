"""Admin API and pages (bearer token). The HTML pages carry no data: they ask for the token and call the JSON API,
so there is no cookie session to steal or forge."""

import hmac
import json
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from defuser.config import get_settings
from defuser.db import get_pool
from defuser.models import Verdict

router = APIRouter(prefix="/admin")
_UI = Path(__file__).parent / "ui.html"


def require_admin(authorization: str | None) -> None:
    token = get_settings().admin_token
    if not token or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401)
    if not hmac.compare_digest(authorization.removeprefix("Bearer "), token.get_secret_value()):
        raise HTTPException(401)


@router.get("/ping")
async def ping(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    return {"ok": True}


@router.get("", response_class=HTMLResponse)
async def ui():
    return HTMLResponse(_UI.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


async def stats(pool, hours: int) -> dict:
    """Checks answered to people (purpose='check') in the window."""
    row = await pool.fetchrow(
        """SELECT count(*) AS checks,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
                  COALESCE(avg(cost_usd), 0) AS avg_cost,
                  COALESCE(sum(cost_usd), 0) AS total_cost,
                  avg(CASE WHEN cache_hit IS NOT NULL AND cache_hit <> 'none' THEN 1.0 ELSE 0.0 END) AS cache_rate,
                  avg(CASE WHEN verifier_passed THEN 1.0 ELSE 0.0 END)
                      FILTER (WHERE verifier_passed IS NOT NULL) AS verifier_pass
           FROM runs WHERE purpose = 'check' AND status <> 'running'
             AND started_at > now() - make_interval(hours => $1)""",
        hours,
    )
    verdicts = {
        r["verdict"]: r["n"]
        for r in await pool.fetch(
            """SELECT verdict, count(*) AS n FROM runs WHERE purpose = 'check' AND verdict IS NOT NULL
               AND started_at > now() - make_interval(hours => $1) GROUP BY verdict""",
            hours,
        )
    }
    hits = {
        r["cache_hit"]: r["n"]
        for r in await pool.fetch(
            """SELECT COALESCE(cache_hit, 'none') AS cache_hit, count(*) AS n FROM runs WHERE purpose = 'check'
               AND status <> 'running' AND started_at > now() - make_interval(hours => $1) GROUP BY 1""",
            hours,
        )
    }
    hourly = [
        {"hour": r["h"].isoformat(), "checks": r["n"], "cost": float(r["c"]), "p95": r["p95"]}
        for r in await pool.fetch(
            """SELECT date_trunc('hour', started_at) AS h, count(*) AS n, COALESCE(sum(cost_usd), 0) AS c,
                      percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
               FROM runs WHERE purpose = 'check' AND status <> 'running'
                 AND started_at > now() - make_interval(hours => $1) GROUP BY 1 ORDER BY 1""",
            hours,
        )
    ]
    other = await pool.fetchrow(
        """SELECT (SELECT count(*) FROM review_items WHERE status = 'open') AS review_open,
                  (SELECT COALESCE(sum(cost_usd), 0) FROM runs WHERE purpose <> 'check'
                     AND started_at > now() - make_interval(hours => $1)) AS background_cost,
                  (SELECT count(*) FROM jobs WHERE status = 'dead') AS dead_jobs,
                  (SELECT count(*) FROM jobs WHERE status = 'queued' AND run_at <= now()) AS queue_depth""",
        hours,
    )
    n = row["checks"] or 0
    return {
        "hours": hours,
        "checks": n,
        "latency_p50_ms": round(row["p50"] or 0),
        "latency_p95_ms": round(row["p95"] or 0),
        "cost_per_check_usd": round(float(row["avg_cost"]), 5),
        "cost_total_usd": round(float(row["total_cost"]) + float(other["background_cost"]), 4),
        "cache_hit_rate": round(float(row["cache_rate"] or 0), 3),
        "verifier_pass_rate": round(float(row["verifier_pass"]), 3)
        if row["verifier_pass"] is not None
        else None,
        "verdicts": verdicts,
        "cache_hits": hits,
        "hourly": hourly,
        "review_open": other["review_open"],
        "dead_jobs": other["dead_jobs"],
        "queue_depth": other["queue_depth"],
        "budget_global_day_usd": get_settings().max_cost_global_day_usd,
    }


@router.get("/stats")
async def get_stats(
    hours: int = Query(24, ge=1, le=24 * 30), authorization: str | None = Header(default=None)
):
    require_admin(authorization)
    return await stats(await get_pool(), hours)


@router.get("/reviews")
async def list_reviews(
    status: str = Query("open", pattern="^(open|approved|corrected|all)$"),
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    rows = await (await get_pool()).fetch(
        """SELECT ri.id, ri.reason, ri.status, ri.corrected_verdict, ri.created_at, r.verdict, r.confidence, r.lang,
                  c.canonical_text, c.ask_count
           FROM review_items ri JOIN runs r ON r.id = ri.run_id LEFT JOIN claims c ON c.id = ri.claim_id
           WHERE $1 = 'all' OR ri.status = $1 ORDER BY ri.created_at DESC LIMIT 200""",
        status,
    )
    return [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]


@router.get("/reviews/{item_id}")
async def get_review(item_id: int, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT ri.*, r.verdict, r.confidence, r.lang, r.trace, c.canonical_text
           FROM review_items ri JOIN runs r ON r.id = ri.run_id LEFT JOIN claims c ON c.id = ri.claim_id
           WHERE ri.id = $1""",
        item_id,
    )
    if row is None:
        raise HTTPException(404)
    evidence = await pool.fetch(
        "SELECT evidence_id, url, publisher, tier, label, left(text, 1500) AS text FROM evidence WHERE run_id = $1 "
        "ORDER BY evidence_id",
        row["run_id"],
    )
    out = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(row).items()}
    out["run_id"] = str(row["run_id"])
    out["evidence"] = [dict(e) for e in evidence]
    return out


class Decision(BaseModel):
    action: str  # approve | correct
    verdict: Verdict | None = None
    note: str = ""


@router.post("/reviews/{item_id}")
async def decide(item_id: int, d: Decision, authorization: str | None = Header(default=None)):
    """Approve the downgraded answer, or record the right verdict. A correction feeds the eval set and takes the
    claim's cached verdict out of service (the next ask re-investigates), because a reviewer's verdict has no cited
    evidence behind it and the bot never sends uncited verdicts."""
    require_admin(authorization)
    if d.action not in ("approve", "correct") or (d.action == "correct" and d.verdict is None):
        raise HTTPException(422, "action must be approve, or correct with a verdict")
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        item = await conn.fetchrow(
            """SELECT ri.id, ri.run_id, ri.claim_id, ri.status, r.lang, c.canonical_text
               FROM review_items ri JOIN runs r ON r.id = ri.run_id LEFT JOIN claims c ON c.id = ri.claim_id
               WHERE ri.id = $1 FOR UPDATE OF ri""",
            item_id,
        )
        if item is None:
            raise HTTPException(404)
        status = "approved" if d.action == "approve" else "corrected"
        await conn.execute(
            "UPDATE review_items SET status = $2, corrected_verdict = $3, reviewer_note = $4, decided_at = now() "
            "WHERE id = $1",
            item_id,
            status,
            d.verdict.value if d.verdict else None,
            d.note[:1000],
        )
        if d.action == "correct" and item["canonical_text"]:
            await conn.execute(
                "INSERT INTO eval_corrections (claim_text, lang, verdict, note, source_run_id) VALUES ($1,$2,$3,$4,$5)",
                item["canonical_text"],
                item["lang"],
                d.verdict.value,
                d.note[:1000],
                item["run_id"],
            )
            await conn.execute(
                "UPDATE claims SET verdict_at = now() - interval '3650 days' WHERE id = $1", item["claim_id"]
            )
    return {"ok": True, "status": status}


async def export_corrections(pool, mark: bool = True) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, claim_text, lang, verdict, note, source_run_id FROM eval_corrections WHERE exported_at IS NULL "
        "ORDER BY id"
    )
    out = [
        {
            "id": f"corr-{r['id']}",
            "message": r["claim_text"],
            "lang": r["lang"] or "en",
            "expected": r["verdict"],
            "note": f"reviewer correction: {r['note']}".strip(),
            "source_run_id": str(r["source_run_id"]) if r["source_run_id"] else None,
            "fixtures": None,  # evidence must be frozen by hand before it can join golden.jsonl
        }
        for r in rows
    ]
    if mark and rows:
        await pool.execute(
            "UPDATE eval_corrections SET exported_at = now() WHERE id = ANY($1::bigint[])",
            [r["id"] for r in rows],
        )
    return out


@router.get("/corrections.jsonl", response_class=PlainTextResponse)
async def corrections(mark: bool = False, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    rows = await export_corrections(await get_pool(), mark=mark)
    return PlainTextResponse("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
