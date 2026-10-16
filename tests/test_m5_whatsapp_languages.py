import hashlib
import hmac
import json

import httpx
import pytest

from defuser import pipeline
from defuser.channels import registry
from defuser.channels.whatsapp import WhatsAppError, parse_webhook, verify_handshake, verify_signature
from defuser.config import get_settings
from defuser.i18n import _load, supported, t
from defuser.llm.fake import ScriptedLLM, text_response
from defuser.llm.provider import set_llm
from tests.conftest import FakeChannel, drain_jobs
from tests.helpers import fixtures, happy_script, triage_json

SECRET = "wa-app-secret"


def wa_msg(text="RBI is banning all 10 rupee coins", mid="wamid.1", frm="919800000001", **extra):
    m = {"from": frm, "id": mid, "timestamp": "1760000000", "type": "text", "text": {"body": text}}
    m.update(extra)
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "1555", "phone_number_id": "PN"},
                            "contacts": [{"wa_id": frm, "profile": {"name": "Asha"}}],
                            "messages": [m],
                        },
                    }
                ],
            }
        ],
    }


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def wa(monkeypatch):
    s = get_settings()
    from pydantic import SecretStr

    monkeypatch.setattr(s, "whatsapp_app_secret", SecretStr(SECRET))
    monkeypatch.setattr(s, "whatsapp_verify_token", SecretStr("verify-me"))
    ch = FakeChannel("whatsapp")
    registry.set_channel("whatsapp", ch)
    yield ch
    registry.set_channel("whatsapp", None)


def test_signature_and_handshake():
    body = b'{"a":1}'
    assert verify_signature(body, sign(body), SECRET)
    assert not verify_signature(body + b" ", sign(body), SECRET)
    assert not verify_signature(body, None, SECRET) and not verify_signature(body, sign(body), None)
    ok = {"hub.mode": "subscribe", "hub.verify_token": "t", "hub.challenge": "123"}
    assert verify_handshake(ok, "t") == "123"
    assert verify_handshake({**ok, "hub.verify_token": "x"}, "t") is None


def test_parse_forwarded_text_image_voice():
    ev = parse_webhook(wa_msg(context={"forwarded": True, "frequently_forwarded": True}))[0]
    assert ev.command == "check" and ev.forwarded and ev.forwarded_many_times and ev.chat_id == "919800000001"
    img = parse_webhook(
        wa_msg(type="image", image={"id": "MEDIA1", "mime_type": "image/jpeg", "caption": "see"})
    )[0]
    assert img.media.kind == "image" and img.media.file_id == "MEDIA1" and img.text == "see"
    voice = parse_webhook(wa_msg(type="audio", audio={"id": "A1", "mime_type": "audio/ogg", "voice": True}))[
        0
    ]
    assert voice.media.kind == "voice"
    assert parse_webhook(wa_msg(type="reaction", reaction={"emoji": "👍"})) == []


def test_status_callbacks_and_malformed_ignored():
    p = wa_msg()
    p["entry"][0]["changes"][0]["value"].pop("messages")
    p["entry"][0]["changes"][0]["value"]["statuses"] = [{"id": "wamid.x", "status": "delivered"}]
    assert parse_webhook(p) == []
    assert parse_webhook({"entry": [{"changes": [{"value": {"messages": [{"type": "text"}]}}]}]}) == []


def test_group_rules():
    assert parse_webhook(wa_msg("good morning", group_id="G1")) == []
    ev = parse_webhook(wa_msg("/check Onions cure flu", group_id="G1"))[0]
    assert ev.is_group and ev.chat_id == "group:G1" and ev.text == "Onions cure flu"
    bare = parse_webhook(wa_msg("/check", group_id="G1", context={"id": "wamid.target"}))[0]
    assert bare.command == "help"  # we can't see the replied-to message


async def test_whatsapp_webhook_end_to_end(clean, wa):
    from defuser.app import create_app

    body = json.dumps(wa_msg(context={"forwarded": True})).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t") as c:
        bad = await c.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=00"})
        assert bad.status_code == 403
        for _ in range(2):  # Meta retries deliveries: the second is a duplicate
            r = await c.post(
                "/webhooks/whatsapp",
                content=body,
                headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"},
            )
            assert r.status_code == 200
        v = await c.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"},
        )
        assert v.text == "42"
    assert await clean.fetchval("SELECT count(*) FROM inbound_messages") == 1
    backends, fetcher = fixtures()
    pipeline.set_deps(backends=backends, fetcher=fetcher)
    set_llm(happy_script())
    try:
        await drain_jobs(clean)
    finally:
        set_llm(None)
        pipeline.set_deps()
    assert len(wa.sent) == 1 and wa.sent[0][0] == "919800000001" and wa.sent[0][1].startswith("❌")
    assert wa.sent[0][2] == "wamid.1"  # threaded as a reply


def test_every_language_has_every_key():
    base = _load("en")
    for code in supported():
        tpl = _load(code)
        assert tpl, code
        for key, val in base.items():
            if key in ("name", "reviewed"):
                continue
            assert key in tpl, f"{code} missing {key}"
            if isinstance(val, dict):
                assert set(val) == set(tpl[key]), f"{code}.{key}"
        assert "1930" in t(code, "next_step", "SCAM") and "cybercrime.gov.in" in t(code, "next_step", "SCAM")


@pytest.mark.parametrize(
    ("lang", "text", "headline"),
    [
        ("ta", "ரிசர்வ் வங்கி 10 ரூபாய் நாணயங்களை தடை செய்கிறது", "❌ இது பொய்யான தகவல்."),
        ("te", "ఆర్‌బీఐ 10 రూపాయల నాణేలను నిషేధిస్తోంది", "❌ ఇది తప్పుడు సమాచారం."),
        ("bn", "আরবিআই ১০ টাকার কয়েন নিষিদ্ধ করছে", "❌ এটা মিথ্যা।"),
        ("mr", "आरबीआय 10 रुपयांची नाणी बंद करत आहे", "❌ ही माहिती खोटी आहे."),
        ("hinglish", "RBI 10 rupaye ke sikke band kar raha hai, sabko bhejo", "❌ Yeh baat"),
    ],
)
async def test_reply_in_senders_language(clean, wa, lang, text, headline):
    inner = happy_script()

    def script(p, kw, n):
        if p == "triage":
            return text_response(triage_json(text=text, lang=lang))
        return inner.script(p, kw, n)

    from defuser.ingest import ingest_event

    backends, fetcher = fixtures()
    pipeline.set_deps(backends=backends, fetcher=fetcher)
    set_llm(ScriptedLLM(script))
    try:
        await ingest_event(clean, parse_webhook(wa_msg(text))[0])
        await drain_jobs(clean)
    finally:
        set_llm(None)
        pipeline.set_deps()
    assert wa.sent[0][1].startswith(headline), wa.sent[0][1][:80]


async def test_closed_whatsapp_window_is_not_retried(clean, wa):
    from defuser.ingest import ingest_event

    class Closed(FakeChannel):
        async def send_text(self, chat_id, text, reply_to=None):
            raise WhatsAppError(400, 131047, "Re-engagement message")

    backends, fetcher = fixtures()
    pipeline.set_deps(backends=backends, fetcher=fetcher)
    try:
        set_llm(happy_script(bad_drafts=2))
        await ingest_event(clean, parse_webhook(wa_msg())[0])
        await drain_jobs(clean)
        assert wa.sent[0][1].startswith("🤔")
        registry.set_channel("whatsapp", Closed("whatsapp"))  # 72 hours later the window has closed
        await clean.execute("UPDATE jobs SET run_at = now() WHERE kind = 'recheck_claim'")
        set_llm(happy_script())
        await drain_jobs(clean)
    finally:
        set_llm(None)
        pipeline.set_deps()
    statuses = [
        r["status"] for r in await clean.fetch("SELECT status FROM jobs WHERE kind = 'recheck_claim'")
    ]
    assert statuses == ["done", "done", "done"]  # not retried into the dead-letter queue
    assert await clean.fetchval("SELECT verdict_sent FROM claim_asks") == "FALSE"
    assert not WhatsAppError(400, 131047, "").retryable and WhatsAppError(503, None, "").retryable
