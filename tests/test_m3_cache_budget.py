import httpx

from defuser import budget, pipeline
from defuser.config import get_settings
from defuser.ingest import ingest_event
from defuser.llm.fake import ScriptedLLM, text_response
from defuser.llm.provider import set_llm
from tests.conftest import drain_jobs
from tests.helpers import event, fixtures, happy_script, triage_json


class Counting:
    """Wraps a scripted LLM and records purposes."""

    def __init__(self, inner):
        self.inner, self.purposes = inner, []

    async def complete(self, **kw):
        self.purposes.append(kw.get("purpose"))
        return await self.inner.complete(**kw)


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


async def test_exact_repeat_answered_from_cache_without_llm(clean, tg):
    llm = Counting(happy_script())
    await _run(clean, llm, event(mid="1"))
    n = len(llm.purposes)
    await _run(clean, llm, event(mid="2", chat_id="222"))
    assert len(llm.purposes) == n  # zero model calls for the repeat
    assert tg.sent[1][1] == tg.sent[0][1].replace(tg.sent[0][1], tg.sent[1][1])  # same verdict rendered
    hits = [r["cache_hit"] for r in await clean.fetch("SELECT cache_hit FROM runs ORDER BY started_at")]
    assert hits == ["none", "text_sha"]
    assert await clean.fetchval("SELECT ask_count FROM claims") == 2


async def test_same_claim_other_language_skips_investigation(clean, tg):
    inner = happy_script()

    def script(p, kw, n):
        if p == "triage" and "हिंदी" in str(kw["messages"]) or p == "triage" and "सिक्के" in str(kw["messages"]):
            return text_response(triage_json(text="आरबीआई 10 रुपये के सिक्के बंद", lang="hi"))
        if p == "same_claim":
            return text_response({"same_claim": True})
        return inner.script(p, kw, n)

    llm = Counting(ScriptedLLM(script))
    await _run(clean, llm, event(mid="1"))
    first = len(llm.purposes)
    await _run(clean, llm, event(text="आरबीआई अगले महीने से 10 रुपये के सभी सिक्के बंद कर रहा है", mid="2"))
    later = llm.purposes[first:]
    assert "investigate" not in later and "triage" in later and "compose1" in later
    assert (
        await clean.fetchval("SELECT cache_hit FROM runs ORDER BY started_at DESC LIMIT 1")
    ) == "claim_embedding"
    assert tg.sent[1][1].startswith("❌ यह बात झूठी है")


async def test_different_claim_not_merged(clean, tg):
    inner = happy_script()

    def script(p, kw, n):
        if p == "triage" and "silver" in str(kw["messages"]):
            return text_response(
                dict(
                    triage_json(),
                    claims=[
                        {
                            "text": "RBI will ban 10 rupee silver coins",
                            "canonical_en": "RBI will ban 10 rupee silver coins",
                        }
                    ],
                )
            )
        if p == "same_claim":
            return text_response({"same_claim": False})
        return inner.script(p, kw, n)

    llm = Counting(ScriptedLLM(script))
    await _run(clean, llm, event(mid="1"), event(text="RBI will ban 10 rupee silver coins", mid="2"))
    assert llm.purposes.count("investigate") >= 8 and llm.purposes.count("same_claim") == 1
    assert await clean.fetchval("SELECT count(*) FROM claims") == 2


async def test_stale_unverified_is_rechecked(clean, tg):
    llm = Counting(happy_script(bad_drafts=2))  # first check ends UNVERIFIED
    await _run(clean, llm, event(mid="1"))
    await clean.execute("UPDATE claims SET verdict_at = now() - interval '7 hours'")
    n = len(llm.purposes)
    await _run(clean, llm, event(mid="2"))
    assert "investigate" in llm.purposes[n:]


async def test_user_daily_budget_returns_try_later(clean, tg):
    s = get_settings()
    old = s.max_cost_per_user_day_usd
    s.max_cost_per_user_day_usd = 0.0001
    try:
        llm = Counting(happy_script())
        await _run(clean, llm, event(mid="1"), event(text="Another claim entirely about onions", mid="2"))
    finally:
        s.max_cost_per_user_day_usd = old
    assert "try again" in tg.sent[1][1].lower()
    assert llm.purposes.count("triage") == 1  # the over-budget message never reached the model


async def test_global_budget_enforced_even_when_redis_down(clean, tg, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "redis_url", "redis://127.0.0.1:1/0")
    budget.reset_redis()
    monkeypatch.setattr(s, "max_cost_global_day_usd", 0.0001)
    try:
        await _run(clean, happy_script(), event(mid="1"), event(text="Second message about salt", mid="2"))
    finally:
        budget.reset_redis()
    assert "try again" in tg.sent[1][1].lower()


async def test_spam_rate_limited_at_webhook(clean, tg):
    from defuser.app import create_app
    from tests.test_m0_telegram import upd

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t") as c:
        for i in range(8):
            await c.post(
                "/webhooks/telegram",
                json=upd(f"spam {i}", mid=100 + i),
                headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
            )
    kinds = [r["kind"] for r in await clean.fetch("SELECT kind FROM inbound_messages ORDER BY id")]
    assert kinds.count("text") == get_settings().max_messages_per_user_minute and kinds.count("command") == 1
