"""Processes one inbound message end to end. Safe to re-run: the run id is derived from the inbound id, so a
resumed job replays its journal, and replies go through the idempotent outbox."""

import logging
import uuid

import asyncpg

from defuser.agent.backends import default_backends
from defuser.channels.base import IncomingEvent
from defuser.channels.registry import get_channel
from defuser.checker import Checker
from defuser.i18n import resolve
from defuser.llm.provider import get_llm
from defuser.models import ChatRef
from defuser.normalize.fetch import Fetcher
from defuser.normalize.message import normalize_event
from defuser.normalize.text import guess_lang
from defuser.observability import CHECK_COST, CHECK_LATENCY, CHECKS
from defuser.outbox import send_once
from defuser.privacy import decrypt_json
from defuser.render import render_simple
from defuser.runs import finish_run, start_run

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
        await send_once(pool, key, chat, render_simple(lang, ev.command))
    elif ev.command == "forget":
        from defuser.privacy_ops import forget_user

        await forget_user(pool, ev.channel, ev.user_id)
        await send_once(pool, key, chat, render_simple(lang, "forget_done"))
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
    await channel.typing(chat.chat_id)

    media_bytes = None
    processor = _deps.get("media_processor")
    if ev.media is not None:
        from defuser.media import MediaProcessor, download_media

        processor = processor or MediaProcessor.default()
        media_bytes = await download_media(channel, ev.media)

    fetcher = _deps.get("fetcher") or Fetcher()
    msg = await normalize_event(ev, chat, fetcher, media_bytes, processor)
    run_id = run_id_for(inbound_id)
    await start_run(pool, run_id, inbound_id, "check")
    checker = make_checker(pool)
    res = await checker.check(msg, run_id=run_id)
    await finish_run(pool, res)
    await send_once(pool, f"reply:{inbound_id}", chat, res.reply)
    CHECKS.labels(res.verdict.value, "none", ev.channel).inc()
    CHECK_LATENCY.observe(res.latency_ms / 1000)
    CHECK_COST.observe(res.meter.cost if res.meter else 0)
    await _mark_done(pool, inbound_id)


async def _mark_done(pool: asyncpg.Pool, inbound_id: int) -> None:
    await pool.execute(
        "UPDATE inbound_messages SET status = 'done', payload_enc = NULL WHERE id = $1", inbound_id
    )
