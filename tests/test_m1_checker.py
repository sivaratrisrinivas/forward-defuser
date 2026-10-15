import pytest

from defuser.checker import Checker
from defuser.llm.fake import ScriptedLLM, text_response
from defuser.llm.provider import LLMError
from defuser.models import Verdict
from tests.helpers import GOOD_DRAFT, fixtures, happy_script, msg, triage_json


async def run(llm, case_fail=None, pool=None, run_id=None, m=None):
    backends, fetcher = fixtures(fail=case_fail)
    return await Checker(llm, backends, fetcher, pool=pool).check(m or msg(), run_id=run_id)


async def test_happy_path_false_with_citations():
    res = await run(happy_script())
    assert res.verdict == Verdict.FALSE and res.report.passed and res.compose_attempts == 1
    assert "❌" in res.reply and "altnews.in" in res.reply and "Sources:" in res.reply
    assert res.evidence["E2"].label.value == "refutes"
    assert not res.needs_review


async def test_bad_draft_regenerated_once():
    res = await run(happy_script(bad_drafts=1))
    assert res.verdict == Verdict.FALSE and res.compose_attempts == 2


async def test_two_bad_drafts_fall_back_to_unverified_and_review():
    res = await run(happy_script(bad_drafts=2))
    assert res.verdict == Verdict.UNVERIFIED and not res.draft.reasons
    assert "🤔" in res.reply and res.needs_review and res.followup


async def test_low_confidence_false_downgraded():
    d = dict(GOOD_DRAFT, confidence=0.5)
    res = await run(happy_script(draft=d))
    assert res.verdict == Verdict.UNVERIFIED and "downgraded" in res.needs_review


async def test_true_on_injected_message_downgraded():
    d = dict(GOOD_DRAFT, verdict="TRUE")
    llm = happy_script(draft=d, verdict="TRUE", triage=triage_json(injection=True))
    res = await run(llm)
    assert res.verdict == Verdict.UNVERIFIED


async def test_opinion_short_circuits():
    llm = happy_script(triage=triage_json(text="Our country is the best", checkable=False, ctype="opinion"))
    res = await run(llm)
    assert res.kind == "opinion" and len(llm.calls) == 1


async def test_llm_down_gives_unverified_not_error():
    llm = ScriptedLLM(lambda p, kw, n: LLMError("503"))
    res = await run(llm)
    assert res.verdict == Verdict.UNVERIFIED and res.reply


async def test_malformed_triage_falls_back():
    def script(p, kw, n):
        if p == "triage":
            return text_response("not json at all {")
        return happy_script().script(p, kw, n)

    res = await run(ScriptedLLM(script))
    assert res.verdict == Verdict.FALSE


@pytest.mark.parametrize(
    "fail", [{"factcheck": "down"}, {"fetch": "down"}, {"factcheck": "junk", "fetch": "junk"}]
)
async def test_tool_outages_never_crash(fail):
    res = await run(happy_script(), case_fail=fail)
    # With the fact-check source unavailable the draft cites missing evidence and is rejected -> UNVERIFIED.
    assert res.verdict in (Verdict.UNVERIFIED, Verdict.FALSE)
    assert res.report is not None


async def test_budget_cap_stops_loop():
    from defuser.config import get_settings

    s = get_settings()
    old = s.max_cost_per_message_usd
    s.max_cost_per_message_usd = 0.0001
    try:
        res = await run(happy_script())
    finally:
        s.max_cost_per_message_usd = old
    assert res.investigation.stop == "budget" and res.verdict == Verdict.UNVERIFIED


async def test_killed_run_resumes_from_journal(clean):
    import uuid

    run_id = str(uuid.uuid4())
    await clean.execute("INSERT INTO runs (id) VALUES ($1)", run_id)
    inner = happy_script()

    class Crash(Exception):
        pass

    crashed = {"done": False}

    def crashing(p, kw, n):
        if p == "investigate" and len(kw["messages"]) == 5 and not crashed["done"]:
            crashed["done"] = True
            raise Crash()
        return inner.script(p, kw, n)

    llm = ScriptedLLM(crashing)
    with pytest.raises(Crash):
        await run(llm, pool=clean, run_id=run_id)
    first_calls = len(llm.calls)
    backends, fetcher = fixtures()
    res = await Checker(llm, backends, fetcher, pool=clean).check(msg(), run_id=run_id)
    assert res.verdict == Verdict.FALSE
    assert res.replayed_steps >= 3  # triage + 2 investigate llm steps + their tools replayed
    # Only the crashed step and later ones were re-requested from the model.
    later = [p for p, _ in llm.calls[first_calls:]]
    assert "triage" not in later and fetcher.fetched == []  # nothing refetched on resume
