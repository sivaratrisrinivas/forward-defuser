from defuser import pipeline
from defuser.ingest import ingest_event
from defuser.llm.provider import set_llm
from tests.conftest import drain_jobs
from tests.helpers import event, fixtures, happy_script


async def test_end_to_end_reply_and_run_persisted(clean, tg):
    backends, fetcher = fixtures()
    pipeline.set_deps(backends=backends, fetcher=fetcher)
    set_llm(happy_script())
    try:
        await ingest_event(clean, event())
        await drain_jobs(clean)
    finally:
        set_llm(None)
        pipeline.set_deps()
    assert len(tg.sent) == 1 and tg.sent[0][1].startswith("❌")
    run = await clean.fetchrow("SELECT verdict, verifier_passed, cost_usd, latency_ms, trace FROM runs")
    assert run["verdict"] == "FALSE" and run["verifier_passed"] and run["cost_usd"] > 0
    assert await clean.fetchval("SELECT count(*) FROM evidence") == 2
    assert await clean.fetchval("SELECT payload_enc FROM inbound_messages") is None  # purged after processing


async def test_commands(clean, tg):
    ev = event(text="/start")
    ev.command = "start"
    await ingest_event(clean, ev)
    ev2 = event(text="/forget", mid="11")
    ev2.command = "forget"
    await ingest_event(clean, ev2)
    await drain_jobs(clean)
    assert "Namaste" in tg.sent[0][1] and "deleted" in tg.sent[1][1]
    assert await clean.fetchval("SELECT count(*) FROM users") == 0
