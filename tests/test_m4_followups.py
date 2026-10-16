from defuser import followup, pipeline
from defuser.config import get_settings
from defuser.ingest import ingest_event
from defuser.llm.fake import ScriptedLLM, text_response
from defuser.llm.provider import set_llm
from defuser.models import ChatRef, Verdict
from defuser.privacy_ops import apply_retention
from tests.conftest import drain_jobs
from tests.helpers import event, fixtures, happy_script, triage_json


async def _run(pool, llm, *events):
    backends, fetcher = fixtures()
    pipeline.set_deps(backends=backends, fetcher=fetcher)
    set_llm(llm)
    try:
        for ev in events:
            await ingest_event(pool, ev)
        await drain_jobs(pool)
    finally:
        set_llm(None)
        pipeline.set_deps()


async def _due(pool, hours=None):
    q = "UPDATE jobs SET run_at = now() WHERE kind = 'recheck_claim' AND status = 'queued'"
    if hours is not None:
        q += f" AND payload->>'hours' = '{hours}'"
    await pool.execute(q)


async def test_unverified_gets_one_update_when_verdict_appears(clean, tg):
    await _run(clean, happy_script(bad_drafts=2), event(mid="1"))
    assert tg.sent[0][1].startswith("🤔")
    keys = sorted(
        r["idempotency_key"]
        for r in await clean.fetch("SELECT idempotency_key FROM jobs WHERE kind='recheck_claim'")
    )
    assert keys == ["recheck:1:24", "recheck:1:6", "recheck:1:72"]

    await _due(clean, 6)
    await _run(clean, happy_script())  # fact-checkers have published now
    assert len(tg.sent) == 2
    assert tg.sent[1][1].startswith("📢") and "❌" in tg.sent[1][1] and tg.sent[1][0] == "111"
    assert await clean.fetchval("SELECT verdict FROM claims WHERE id = 1") == "FALSE"
    assert await clean.fetchval("SELECT verdict_sent FROM claim_asks") == "FALSE"
    assert await clean.fetchval("SELECT ask_count FROM claims WHERE id = 1") == 1  # a recheck is not an ask

    # Later re-checks and a re-run of the notifier never send it twice.
    await _due(clean)
    await _run(clean, happy_script())
    set_llm(happy_script())
    try:
        assert await followup.notify_subscribers(clean, 1, pipeline.make_checker(clean)) == 0
    finally:
        set_llm(None)
    assert len(tg.sent) == 2


async def test_still_unverified_sends_nothing(clean, tg):
    await _run(clean, happy_script(bad_drafts=2), event(mid="1"))
    await _due(clean)
    await _run(clean, happy_script(bad_drafts=2))
    assert len(tg.sent) == 1
    assert await clean.fetchval("SELECT count(*) FROM runs WHERE purpose = 'recheck'") == 3


async def test_update_is_in_the_askers_language(clean, tg):
    inner = happy_script(bad_drafts=2)

    def first(p, kw, n):
        if p == "triage":
            return text_response(triage_json(text="आरबीआई 10 रुपये के सिक्के बंद", lang="hi"))
        return inner.script(p, kw, n)

    await _run(
        clean, ScriptedLLM(first), event(text="आरबीआई अगले महीने से 10 रुपये के सभी सिक्के बंद कर रहा है", mid="1")
    )
    assert await clean.fetchval("SELECT lang FROM claim_asks") == "hi"
    await _due(clean, 6)
    await _run(clean, happy_script())
    assert tg.sent[1][1].startswith("📢") and "❌ यह बात झूठी है" in tg.sent[1][1]


async def test_forget_stops_followups(clean, tg):
    # Private chat: Telegram's chat id equals the user id.
    await _run(clean, happy_script(bad_drafts=2), event(mid="1", chat_id="5"))
    forget = event(text="/forget", mid="2", chat_id="5").model_copy(update={"command": "forget"})
    await _run(clean, happy_script(), forget)
    await _due(clean)
    await _run(clean, happy_script())
    assert [m[1] for m in tg.sent][-1].startswith("Done.")
    assert len(tg.sent) == 2
    assert await clean.fetchval("SELECT count(*) FROM claim_asks") == 0
    keys = [r["idempotency_key"] for r in await clean.fetch("SELECT idempotency_key FROM outbox")]
    assert keys == ["reply:2"]  # the first reply's record is gone; only the forget confirmation remains


async def test_verified_reply_keeps_no_chat_pointer(clean, tg):
    await _run(clean, happy_script(), event(mid="1"))
    row = await clean.fetchrow("SELECT chat_ref_enc, verdict_sent FROM claim_asks")
    assert row["verdict_sent"] == "FALSE" and row["chat_ref_enc"] is None
    assert await clean.fetchval("SELECT count(*) FROM jobs WHERE kind = 'recheck_claim'") == 0


async def test_viral_alert_fires_once(clean, tg, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "viral_threshold_24h", 3)
    monkeypatch.setattr(s, "admin_telegram_chat_id", "999")
    cid = await clean.fetchval(
        "INSERT INTO claims (canonical_text, claim_type, verdict, embedding) VALUES ('Onions cure flu', 'health', 'FALSE', array_fill(0.1::real, ARRAY[256])::vector) RETURNING id"
    )
    fired = []
    for u in range(5):
        await followup.subscribe(
            clean, cid, f"user{u}", ChatRef(channel="telegram", chat_id=str(u)), "en", Verdict.FALSE
        )
        fired.append(await followup.maybe_alert_viral(clean, cid))
    assert fired == [False, False, True, False, False]
    await drain_jobs(clean)
    await drain_jobs(clean)
    admin = [m for m in tg.sent if m[0] == "999"]
    assert len(admin) == 1 and "3 people" in admin[0][1] and "Onions cure flu" in admin[0][1]


async def test_retention_and_maintenance(clean, tg):
    await _run(clean, happy_script(bad_drafts=2), event(mid="1"))
    await clean.execute("UPDATE claim_asks SET asked_at = now() - interval '5 days'")
    await apply_retention(clean, 30)
    assert (
        await clean.fetchval("SELECT chat_ref_enc FROM claim_asks") is None
    )  # pointer gone after the window
    await clean.execute("UPDATE claim_asks SET asked_at = now() - interval '31 days'")
    await clean.execute("UPDATE runs SET started_at = now() - interval '31 days'")
    await clean.execute("UPDATE inbound_messages SET received_at = now() - interval '31 days'")

    from defuser.jobs.queue import enqueue

    await enqueue(clean, "maintenance", {}, "maintenance:test")
    await drain_jobs(clean)
    assert await clean.fetchval("SELECT count(*) FROM claim_asks") == 0
    assert await clean.fetchval("SELECT count(*) FROM runs") == 0
    assert await clean.fetchval("SELECT count(*) FROM inbound_messages") == 0
    assert (
        await clean.fetchval("SELECT count(*) FROM jobs WHERE kind = 'maintenance' AND status = 'queued'")
        == 1
    )
