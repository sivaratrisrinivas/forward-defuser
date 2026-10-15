"""Processes one inbound message end to end. Safe to re-run: the run id is derived from the inbound id, so a
resumed job replays its journal, and replies go through the idempotent outbox."""

import logging
import uuid

import asyncpg

from defuser import budget, singleflight
from defuser.agent.backends import default_backends
from defuser.cache import ClaimCache
from defuser.channels.base import IncomingEvent
from defuser.channels.registry import get_channel
from defuser.checker import Checker
from defuser.i18n import resolve
from defuser.llm.provider import get_llm
from defuser.models import ChatRef, Verdict
from defuser.normalize.fetch import Fetcher
from defuser.normalize.message import normalize_event
from defuser.normalize.text import guess_lang
from defuser.observability import CHECK_COST, CHECK_LATENCY, CHECKS
from defuser.outbox import send_once
from defuser.privacy import decrypt_json, hash_id
from defuser.render import render_simple
from defuser.runs import finish_run, start_run
from defuser.singleflight import flight_key

log = logging.getLogger(__name__)
RUN_NS = uuid.UUID("6f1c1d2e-8a51-4f0a-9d0e-5b7b8f6c2a10")

_deps: dict = {}


def set_deps(**kw) -> None:
    """Tests and evals inject backends / fetcher / media processor here."""
    _deps.clear()
    _deps.update(kw)


def run_id_for(inbound_id: int) -> str:
    return str(uuid.uuid5(RUN_NS, f"inbound:{inbound_id}"))


def make_checker(pool) -> Checker:
    return Checker(
        get_llm(),
        _deps.get("backends") or default_backends(),
        _deps.get("fetcher") or Fetcher(),
        pool=pool,
        image_backend=_deps.get("image_backend"),
    )


async def handle_command(
    pool: asyncpg.Pool, inbound_id: int, ev: IncomingEvent, chat: ChatRef, lang: str
) -> None:
    key = f"reply:{inbound_id}"
    if ev.command in ("start", "help"):
        which = "help_wa_group" if ev.channel == "whatsapp" and ev.is_group else ev.command
        await send_once(pool, key, chat, render_simple(lang, which))
    elif ev.command == "forget":
        from defuser.privacy_ops import forget_user

        await forget_user(pool, ev.channel, ev.user_id)
        await send_once(pool, key, chat, render_simple(lang, "forget_done"))
    elif ev.command == "rate_limited":
        await send_once(pool, key, chat, render_simple(lang, "rate_limited"))
    elif ev.command == "lang":
        await send_once(pool, key, chat, render_simple(lang, "help"))


async def process_inbound_message(pool: asyncpg.Pool, inbound_id: int) -> None:
    row = await pool.fetchrow(
        "SELECT id, payload_enc, status FROM inbound_messages WHERE id = $1", inbound_id
    )
    if row is None or row["payload_enc"] is None or row["status"] in ("done", "ignored"):
        return
    data = decrypt_json(row["payload_enc"])
    ev, chat = IncomingEvent(**data["event"]), ChatRef(**data["chat"])
    await pool.execute("UPDATE inbound_messages SET status = 'processing' WHERE id = $1", inbound_id)
    lang_guess = resolve(guess_lang(ev.text, ev.lang_hint))

    if ev.command not in (None, "check"):
        await handle_command(pool, inbound_id, ev, chat, lang_guess)
        await _mark_done(pool, inbound_id)
        return

    channel = get_channel(ev.channel)
    await channel.typing(chat.chat_id, ev.message_id)

    media_bytes = None
    processor = _deps.get("media_processor")
    if ev.media is not None:
        from defuser.media import MediaProcessor, download_media

        processor = processor or MediaProcessor.default()
        media_bytes = await download_media(channel, ev.media)

    user_hash = hash_id(ev.channel, ev.user_id)
    allowance = await budget.reserve(pool, user_hash)
    if not allowance.ok:
        await send_once(pool, f"reply:{inbound_id}", chat, render_simple(lang_guess, "busy"))
        await _mark_done(pool, inbound_id)
        return

    fetcher = _deps.get("fetcher") or Fetcher()
    msg = await normalize_event(ev, chat, fetcher, media_bytes, processor)
    run_id = run_id_for(inbound_id)
    await start_run(pool, run_id, inbound_id, "check")
    checker = make_checker(pool)
    cache = ClaimCache(pool)

    res = None
    hit = await cache.by_fingerprint(msg)
    sf_key = flight_key(msg) if hit is None else None
    token = await singleflight.acquire(sf_key) if sf_key else None
    if sf_key and token is None:
        # Someone is already checking this exact forward: wait for their answer instead of paying twice.
        hit = await singleflight.wait_for(sf_key, lambda: cache.by_fingerprint(msg))
        if hit is None:
            token = await singleflight.acquire(sf_key)
    try:
        res = await _check_and_record(checker, cache, msg, hit, run_id, allowance)
    finally:
        await singleflight.release(sf_key, token)
    await finish_run(pool, res, cache_hit=res.cache_hit, claim_id=res.claim_id)
    await budget.record_spend(user_hash, res.meter.cost if res.meter else 0.0)
    await send_once(pool, f"reply:{inbound_id}", chat, res.reply)
    await after_reply(pool, res, msg, chat, inbound_id)
    CHECKS.labels(res.verdict.value, res.cache_hit, ev.channel).inc()
    CHECK_LATENCY.observe(res.latency_ms / 1000)
    CHECK_COST.observe(res.meter.cost if res.meter else 0)
    await _mark_done(pool, inbound_id)


async def _check_and_record(checker, cache, msg, hit, run_id, allowance):
    res = None
    if hit is not None:
        res = await checker.from_cache(hit, msg, hit.lang or msg.lang, run_id)
    if res is None:
        res = await checker.check(msg, run_id=run_id, max_cost=allowance.max_cost, cache=cache)

    claim_id = res.claim_id
    if res.kind == "checked" and res.triage and res.triage.claims or res.cache_hit != "none":
        canonical = (
            res.triage.claims[0].canonical_en
            if res.triage and res.triage.claims
            else msg.combined_text()[:500]
        )
        claim_type = res.triage.claim_type.value if res.triage else "other"
        claim_id = await cache.record(
            canonical,
            claim_type,
            msg,
            res.verdict,
            res.confidence,
            run_id,
            update_verdict=res.cache_hit == "none",
            claim_id=res.claim_id,
        )
        res.claim_id = claim_id
    return res


async def after_reply(pool, res, msg, chat, inbound_id) -> None:
    """Remember who asked (for viral alerts), schedule re-checks for UNVERIFIED claims, alert admins on spikes.
    Failures here must not fail the job: the reply is already sent."""
    from defuser import followup

    if res.claim_id is None or res.kind != "checked":
        return
    try:
        await followup.subscribe(pool, res.claim_id, msg.user_hash, chat, res.lang, res.verdict)
        if res.verdict == Verdict.UNVERIFIED:
            await followup.schedule_rechecks(pool, res.claim_id)
        await followup.maybe_alert_viral(pool, res.claim_id)
    except Exception:  # noqa: BLE001
        log.exception("after_reply failed for inbound %s", inbound_id)


async def send_error_reply(pool: asyncpg.Pool, inbound_id: int) -> None:
    row = await pool.fetchrow("SELECT payload_enc FROM inbound_messages WHERE id = $1", inbound_id)
    if row is None or row["payload_enc"] is None:
        return
    data = decrypt_json(row["payload_enc"])
    ev, chat = IncomingEvent(**data["event"]), ChatRef(**data["chat"])
    await send_once(
        pool, f"reply:{inbound_id}", chat, render_simple(resolve(guess_lang(ev.text, ev.lang_hint)), "error")
    )
    await pool.execute(
        "UPDATE inbound_messages SET status = 'failed', payload_enc = NULL WHERE id = $1", inbound_id
    )


async def _mark_done(pool: asyncpg.Pool, inbound_id: int) -> None:
    await pool.execute(
        "UPDATE inbound_messages SET status = 'done', payload_enc = NULL WHERE id = $1", inbound_id
    )
