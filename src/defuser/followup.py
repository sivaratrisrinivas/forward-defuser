"""Follow-up engine: subscriptions, re-checks of UNVERIFIED claims, update notifications, viral alerts, and the
daily maintenance job. Everything is keyed for idempotency so re-runs and restarts never double-send."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg

from defuser.agent.journal import Journal
from defuser.agent.meter import Meter
from defuser.cache import ClaimCache
from defuser.config import get_settings
from defuser.i18n import t
from defuser.jobs.queue import enqueue
from defuser.models import CanonicalMessage, ChatRef, Claim, TriageResult, Verdict
from defuser.outbox import send_once
from defuser.privacy import decrypt_json, encrypt_json
from defuser.runs import finish_run, start_run

log = logging.getLogger(__name__)
NS = uuid.UUID("0b8a7a8e-2b7f-4a53-8a8e-6c1f0b3b8d21")


async def subscribe(
    pool: asyncpg.Pool, claim_id: int, user_hash: str, chat: ChatRef, lang: str, verdict: Verdict
) -> None:
    """Record that this person asked about the claim (feeds viral alerts). The reversible chat pointer is kept only
    when there is something to follow up on (UNVERIFIED), and retention clears it after the re-check window."""
    chat_enc = encrypt_json(chat.model_dump()) if verdict == Verdict.UNVERIFIED else None
    await pool.execute(
        """INSERT INTO claim_asks (claim_id, user_hash, channel, chat_ref_enc, lang, verdict_sent)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (claim_id, user_hash) DO UPDATE SET chat_ref_enc = EXCLUDED.chat_ref_enc, lang = EXCLUDED.lang,
               verdict_sent = EXCLUDED.verdict_sent, asked_at = now()""",
        claim_id,
        user_hash,
        chat.channel,
        chat_enc,
        lang,
        verdict.value,
    )


async def schedule_rechecks(pool: asyncpg.Pool, claim_id: int) -> list[int]:
    ids = []
    now = datetime.now(UTC)
    for h in get_settings().followup_schedule():
        jid = await enqueue(
            pool,
            "recheck_claim",
            {"claim_id": claim_id, "hours": h},
            f"recheck:{claim_id}:{h}",
            run_at=now + timedelta(hours=h),
            max_attempts=3,
        )
        if jid:
            ids.append(jid)
    return ids


async def maybe_alert_viral(pool: asyncpg.Pool, claim_id: int) -> bool:
    s = get_settings()
    n = await pool.fetchval(
        "SELECT count(*) FROM claim_asks WHERE claim_id = $1 AND asked_at > now() - interval '24 hours'",
        claim_id,
    )
    if n < s.viral_threshold_24h:
        return False
    row = await pool.fetchrow("SELECT canonical_text, verdict FROM claims WHERE id = $1", claim_id)
    key = f"viral:{claim_id}:{datetime.now(UTC):%Y%m%d}"
    body = (
        f"🔥 Viral forward: {n} people asked about this in the last 24h.\nClaim #{claim_id}: "
        f"{row['canonical_text'][:300]}\nCurrent verdict: {row['verdict']}"
    )
    inserted = await pool.fetchval(
        "INSERT INTO admin_alerts (dedupe_key, claim_id, body) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING RETURNING 1",
        key,
        claim_id,
        body,
    )
    if inserted and s.admin_telegram_chat_id:
        await enqueue(pool, "send_admin_alert", {"key": key}, f"alert:{key}")
    return bool(inserted)


async def send_admin_alert(pool: asyncpg.Pool, key: str) -> None:
    s = get_settings()
    body = await pool.fetchval("SELECT body FROM admin_alerts WHERE dedupe_key = $1", key)
    if body and s.admin_telegram_chat_id:
        await send_once(
            pool, f"alert:{key}", ChatRef(channel="telegram", chat_id=s.admin_telegram_chat_id), body
        )


async def recheck_claim(pool: asyncpg.Pool, claim_id: int, hours: int, checker) -> str:
    """Re-investigate an UNVERIFIED claim. Returns what happened (for logs/tests)."""
    claim = await pool.fetchrow(
        "SELECT id, canonical_text, claim_type, verdict FROM claims WHERE id = $1", claim_id
    )
    if claim is None:
        return "gone"
    cache = ClaimCache(pool)
    if claim["verdict"] == "UNVERIFIED":
        run_id = str(uuid.uuid5(NS, f"recheck:{claim_id}:{hours}"))
        await start_run(pool, run_id, None, "recheck", claim_id)
        msg = CanonicalMessage(
            channel="recheck",
            message_id=run_id,
            user_hash="system",
            chat=ChatRef(channel="recheck", chat_id="-"),
            text=claim["canonical_text"],
            lang="en",
        )
        res = await checker.check(msg, run_id=run_id)
        await finish_run(pool, res, claim_id=claim_id)
        if res.verdict == Verdict.UNVERIFIED:
            await pool.execute("UPDATE claims SET checked_at = now() WHERE id = $1", claim_id)
            return "still_unverified"
        await cache.record(
            claim["canonical_text"],
            claim["claim_type"],
            msg,
            res.verdict,
            res.confidence,
            run_id,
            update_verdict=True,
            claim_id=claim_id,
        )
        await pool.execute(
            "UPDATE claims SET ask_count = ask_count - 1 WHERE id = $1", claim_id
        )  # not a real ask
    await notify_subscribers(pool, claim_id, checker)
    return "notified"


async def notify_subscribers(pool: asyncpg.Pool, claim_id: int, checker) -> int:
    cache = ClaimCache(pool)
    hit = await cache.load(claim_id, 1.0)
    if hit is None or hit.verdict == Verdict.UNVERIFIED:
        return 0
    claim = await pool.fetchrow("SELECT canonical_text FROM claims WHERE id = $1", claim_id)
    tri = TriageResult(claims=[Claim(text=claim["canonical_text"], canonical_en=claim["canonical_text"])])
    asks = await pool.fetch(
        "SELECT id, user_hash, chat_ref_enc, lang FROM claim_asks WHERE claim_id = $1 AND verdict_sent = 'UNVERIFIED' "
        "AND chat_ref_enc IS NOT NULL",
        claim_id,
    )
    rendered: dict[str, str] = {}
    sent = 0
    for a in asks:
        lang = a["lang"]
        if lang not in rendered:
            run_id = str(uuid.uuid5(NS, f"notify:{claim_id}:{hit.verdict.value}:{lang}"))
            await start_run(pool, run_id, None, "notify", claim_id)
            journal = await Journal(pool, run_id).load()
            s = get_settings()
            meter = Meter(max_cost=s.max_cost_per_message_usd, max_steps=1)
            msg = CanonicalMessage(
                channel="recheck",
                message_id=run_id,
                user_hash="system",
                chat=ChatRef(channel="recheck", chat_id="-"),
                text=claim["canonical_text"],
            )
            res = await checker._from_cache(hit, msg, lang, run_id, journal, meter, tri)
            if res is None:
                return sent
            rendered[lang] = t(lang, "update") + "\n\n" + res.reply
        chat = ChatRef(**decrypt_json(a["chat_ref_enc"]))
        try:
            await send_once(
                pool, f"followup:{claim_id}:{a['user_hash']}:{hit.verdict.value}", chat, rendered[lang]
            )
            sent += 1
        except Exception as e:
            if getattr(e, "retryable", True):
                raise
            # e.g. WhatsApp's 24-hour window has closed: this person can't be messaged, so don't retry forever.
            log.warning("follow-up to ask %s not deliverable: %s", a["id"], e)
        await pool.execute(
            "UPDATE claim_asks SET verdict_sent = $2 WHERE id = $1", a["id"], hit.verdict.value
        )
    return sent


async def schedule_maintenance(pool: asyncpg.Pool) -> None:
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).replace(
        hour=21, minute=0, second=0, microsecond=0
    )  # ~02:30 IST
    await enqueue(pool, "maintenance", {}, f"maintenance:{tomorrow:%Y%m%d}", run_at=tomorrow, max_attempts=3)
