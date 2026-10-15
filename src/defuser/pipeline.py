"""M0: echo pipeline. Replaced by the full check pipeline in M1."""

import asyncpg

from defuser.channels.base import IncomingEvent
from defuser.models import ChatRef
from defuser.outbox import send_once
from defuser.privacy import decrypt_json


async def process_inbound_message(pool: asyncpg.Pool, inbound_id: int) -> None:
    row = await pool.fetchrow(
        "SELECT id, payload_enc, status FROM inbound_messages WHERE id = $1", inbound_id
    )
    if row is None or row["payload_enc"] is None:
        return
    data = decrypt_json(row["payload_enc"])
    ev, chat = IncomingEvent(**data["event"]), ChatRef(**data["chat"])
    await send_once(pool, f"reply:{inbound_id}", chat, f"echo: {ev.text or '(media)'}")
    await pool.execute(
        "UPDATE inbound_messages SET status = 'done', payload_enc = NULL WHERE id = $1", inbound_id
    )
