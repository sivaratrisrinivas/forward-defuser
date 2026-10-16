from defuser.channels.base import IncomingEvent
from defuser.evals.fixtures import backends_from_case
from defuser.llm.fake import ScriptedLLM, text_response, tool_response
from defuser.llm.provider import LLMError
from defuser.models import CanonicalMessage, ChatRef

ARTICLE = "https://www.altnews.in/fact-check/rbi-10-rupee-coin-ban-false/"
ARTICLE_TEXT = (
    "A message claiming the RBI will ban ten rupee coins is going viral on WhatsApp. Alt News found that the "
    "claim is false. The Reserve Bank of India has clarified that all ten rupee coins in circulation are legal "
    "tender and must be accepted. The rumour has circulated since 2016."
)
CASE = {
    "factchecks": [
        {
            "publisher": "Alt News",
            "url": ARTICLE,
            "claim": "RBI will ban 10 rupee coins",
            "rating": "False",
            "title": "RBI is not banning 10 rupee coins",
            "review_date": "2024-01-02",
        }
    ],
    "search": [],
    "pages": {ARTICLE: ARTICLE_TEXT},
}


def msg(
    text="RBI is banning all 10 rupee coins from next month, forward to all", lang="en", **kw
) -> CanonicalMessage:
    return CanonicalMessage(
        channel="telegram",
        message_id="1",
        user_hash="u",
        chat=ChatRef(channel="telegram", chat_id="1"),
        text=text,
        lang=lang,
        **kw,
    )


def triage_json(
    text="RBI is banning all 10 rupee coins", lang="en", checkable=True, ctype="finance_scam", injection=False
):
    return {
        "claims": [{"text": text, "canonical_en": "RBI will ban 10 rupee coins"}] if text else [],
        "claim_type": ctype,
        "checkable": checkable,
        "lang": lang,
        "injection_suspected": injection,
    }


GOOD_DRAFT = {
    "verdict": "FALSE",
    "confidence": 0.92,
    "reasons": [
        {
            "text": "RBI has said every ten rupee coin is still valid money.",
            "citations": [
                {
                    "evidence_id": "E2",
                    "quote": "all ten rupee coins in circulation are legal tender and must be accepted",
                }
            ],
        },
        {
            "text": "Alt News checked this rumour and found it false.",
            "citations": [{"evidence_id": "E2", "quote": "Alt News found that the claim is false."}],
        },
    ],
}


def happy_script(draft=GOOD_DRAFT, verdict="FALSE", confidence=0.9, triage=None, bad_drafts=0):
    def script(purpose, kw, n):
        if purpose == "triage":
            return text_response(triage or triage_json())
        if purpose == "investigate":
            steps = [
                tool_response(("search_factchecks", {"query": "RBI ban 10 rupee coin", "language": "en"})),
                tool_response(("fetch_url", {"url": ARTICLE})),
                tool_response(
                    ("save_evidence", {"evidence_id": "E2", "label": "refutes", "note": "RBI clarification"})
                ),
                tool_response(
                    (
                        "finish_investigation",
                        {"verdict": verdict, "confidence": confidence, "rationale": "RBI says valid"},
                    )
                ),
            ]
            # Position in the conversation, not call count, so replays and retries line up.
            i = sum(1 for m in kw["messages"] if m["role"] == "assistant")
            return steps[i] if i < len(steps) else LLMError("too many")
        if purpose.startswith("compose"):
            if purpose == "compose1" and bad_drafts >= 1:
                return text_response(
                    {
                        "verdict": "FALSE",
                        "confidence": 0.9,
                        "reasons": [
                            {
                                "text": "RBI banned nothing.",
                                "citations": [
                                    {"evidence_id": "E2", "quote": "RBI never banned any coin at all ever"}
                                ],
                            }
                        ],
                    }
                )
            if purpose == "compose2" and bad_drafts >= 2:
                return text_response({"verdict": "FALSE", "confidence": 0.9, "reasons": []})
            return text_response(draft)
        return LLMError(f"unexpected {purpose}")

    return ScriptedLLM(script)


def event(text="RBI is banning all 10 rupee coins", chat_id="111", mid="10", **kw):
    return IncomingEvent(
        channel="telegram",
        chat_id=chat_id,
        chat_type="private",
        message_id=mid,
        user_id="5",
        command="check",
        text=text,
        reply_to_message_id=mid,
        **kw,
    )


def fixtures(case=CASE, fail=None):
    return backends_from_case(case, fail)
