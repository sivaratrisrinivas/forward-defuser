import httpx

from defuser import pipeline
from defuser.admin.routes import export_corrections
from defuser.app import create_app
from defuser.ingest import ingest_event
from defuser.llm.provider import set_llm
from tests.conftest import drain_jobs
from tests.helpers import GOOD_DRAFT, event, fixtures, happy_script, triage_json

AUTH = {"Authorization": "Bearer admintoken"}


async def _seed(pool):
    backends, fetcher = fixtures()
    pipeline.set_deps(backends=backends, fetcher=fetcher)
    try:
        set_llm(happy_script())
        await ingest_event(pool, event(mid="1"))
        await ingest_event(pool, event(mid="2", chat_id="222"))  # cache hit
        await drain_jobs(pool)
        other = dict(
            triage_json(),
            claims=[{"text": "Banks refuse coins", "canonical_en": "Banks refuse 10 rupee coins"}],
        )
        set_llm(
            happy_script(draft=dict(GOOD_DRAFT, confidence=0.5), triage=other)
        )  # low-confidence FALSE: downgraded, sent to review
        await ingest_event(pool, event(text="RBI will stop accepting 10 rupee coins in banks", mid="3"))
        await drain_jobs(pool)
    finally:
        set_llm(None)
        pipeline.set_deps()


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t")


async def test_admin_requires_token(clean):
    async with client() as c:
        assert (await c.get("/admin/stats")).status_code == 401
        assert (await c.get("/admin/stats", headers={"Authorization": "Bearer nope"})).status_code == 401
        page = await c.get("/admin")
    assert page.status_code == 200 and "Admin token" in page.text and "RBI" not in page.text


async def test_dashboard_stats(clean, tg):
    await _seed(clean)
    async with client() as c:
        s = (await c.get("/admin/stats", headers=AUTH)).json()
    assert s["checks"] == 3
    assert s["verdicts"].get("FALSE") == 2 and sum(s["verdicts"].values()) == 3
    assert abs(s["cache_hit_rate"] - 1 / 3) < 0.01 and s["cache_hits"]["text_sha"] == 1
    assert s["latency_p95_ms"] >= s["latency_p50_ms"] >= 0 and s["cost_per_check_usd"] > 0
    assert s["review_open"] == 1 and len(s["hourly"]) == 1


async def test_review_correction_feeds_evals_and_invalidates_cache(clean, tg):
    await _seed(clean)
    async with client() as c:
        items = (await c.get("/admin/reviews", headers=AUTH)).json()
        assert len(items) == 1 and items[0]["verdict"] == "UNVERIFIED"
        item = (await c.get(f"/admin/reviews/{items[0]['id']}", headers=AUTH)).json()
        assert item["evidence"] and item["trace"]["investigator"]["verdict"] == "FALSE"
        bad = await c.post(f"/admin/reviews/{items[0]['id']}", json={"action": "correct"}, headers=AUTH)
        assert bad.status_code == 422
        r = await c.post(
            f"/admin/reviews/{items[0]['id']}",
            json={"action": "correct", "verdict": "FALSE", "note": "RBI denied"},
            headers=AUTH,
        )
        assert r.json()["status"] == "corrected"
        assert (await c.get("/admin/reviews", headers=AUTH)).json() == []
        jsonl = (await c.get("/admin/corrections.jsonl", headers=AUTH)).text
    assert '"expected": "FALSE"' in jsonl and "RBI denied" in jsonl
    rows = await export_corrections(clean)
    assert len(rows) == 1 and await export_corrections(clean) == []  # marked exported
    stale = await clean.fetchval(
        "SELECT verdict_at < now() - interval '1 year' FROM claims WHERE id = $1",
        await clean.fetchval("SELECT claim_id FROM review_items"),
    )
    assert stale  # the next ask re-investigates instead of serving the reviewed answer
