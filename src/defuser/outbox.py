"""Idempotent outbound messages.

Every reply has an idempotency key derived from what it answers (e.g. `reply:<inbound id>` or
`followup:<claim>:<user>:<verdict>`). The row is inserted before sending and marked `sent` right after the
channel acknowledges, so a re-run job (duplicate webhook, worker killed and resumed) finds `sent` and skips.

The one unavoidable window: if the process dies after the channel accepted the message but before the
`sent` update commits, the retry sends again. We choose at-least-once there (a rare duplicate beats a lost
answer for someone waiting on a verdict); the window is a single UPDATE round-trip.
"""

import logging

import asyncpg

from defuser.channels.registry import get_channel
from defuser.models import ChatRef
from defuser.privacy import decrypt_json, encrypt_json, hash_id

log = logging.getLogger(__name__)


async def send_once(pool: asyncpg.Pool, key: str, chat: ChatRef, text: str) -> str | None:
    """Send `text` to `chat` exactly once per `key`. Returns the provider message id (None if already sent)."""
    await pool.execute(
        """INSERT INTO outbox (idempotency_key, channel, chat_ref_enc, body, chat_hash)
           VALUES ($1, $2, $3, $4, $5) ON CONFLICT (idempotency_key) DO NOTHING""",
        key,
        chat.channel,
        encrypt_json(chat.model_dump()),
        {"text": text},
        hash_id(chat.channel, chat.chat_id),
    )
    row = await pool.fetchrow(
        "UPDATE outbox SET status = 'sending', attempts = attempts + 1 WHERE idempotency_key = $1 AND status <> 'sent' "
        "RETURNING id, chat_ref_enc, body",
        key,
    )
    if row is None:
        return None  # already sent
    ref = ChatRef(**decrypt_json(row["chat_ref_enc"]))
    channel = get_channel(ref.channel)
    try:
        provider_id = await channel.send_text(ref.chat_id, row["body"]["text"], ref.reply_to_message_id)
    except Exception as e:
        await pool.execute(
            "UPDATE outbox SET status = 'failed', last_error = $2 WHERE id = $1",
            row["id"],
            f"{type(e).__name__}: {e}",
        )
        raise
    await pool.execute(
        "UPDATE outbox SET status = 'sent', sent_at = now(), provider_message_id = $2 WHERE id = $1",
        row["id"],
        provider_id,
    )
    return provider_id
