"""Webhook-side work: dedupe, store the minimum, enqueue. Must stay fast (webhooks time out and retry)."""

import asyncpg

from defuser.channels.base import IncomingEvent
from defuser.jobs.queue import enqueue
from defuser.models import ChatRef
from defuser.privacy import encrypt_json, hash_id


async def ingest_event(pool: asyncpg.Pool, ev: IncomingEvent) -> int | None:
    """Returns the inbound id when newly queued, None for a duplicate delivery."""
    chat_hash = hash_id(ev.channel, ev.chat_id)
    user_hash = hash_id(ev.channel, ev.user_id)
    chat = ChatRef(
        channel=ev.channel,
        chat_id=ev.chat_id,
        reply_to_message_id=ev.reply_to_message_id,
        is_group=ev.is_group,
    )
    async with pool.acquire() as conn, conn.transaction():
        inbound_id = await conn.fetchval(
            """INSERT INTO inbound_messages (channel, chat_hash, message_id, user_hash, kind, payload_enc)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (channel, chat_hash, message_id) DO NOTHING RETURNING id""",
            ev.channel,
            chat_hash,
            ev.message_id,
            user_hash,
            ev.kind,
            encrypt_json({"event": ev.model_dump(), "chat": chat.model_dump()}),
        )
        if inbound_id is None:
            return None
        await conn.execute(
            """INSERT INTO users (user_hash, channel) VALUES ($1, $2)
               ON CONFLICT (user_hash) DO UPDATE SET last_seen_at = now()""",
            user_hash,
            ev.channel,
        )
        await enqueue(conn, "process_inbound", {"inbound_id": inbound_id}, f"inbound:{inbound_id}")
    return inbound_id
