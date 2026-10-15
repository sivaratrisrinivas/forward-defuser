"""Delete-my-data and retention."""

import asyncpg

from defuser.privacy import hash_id


async def forget_user(pool: asyncpg.Pool, channel: str, user_id: str) -> dict:
    user_hash = hash_id(channel, user_id)
    async with pool.acquire() as conn, conn.transaction():
        asks = await conn.execute("DELETE FROM claim_asks WHERE user_hash = $1", user_hash)
        inbound = await conn.execute(
            "UPDATE inbound_messages SET payload_enc = NULL, user_hash = 'deleted' WHERE user_hash = $1",
            user_hash,
        )
        await conn.execute("DELETE FROM users WHERE user_hash = $1", user_hash)
    return {"claim_asks": asks, "inbound": inbound}


async def apply_retention(pool: asyncpg.Pool, days: int) -> dict:
    out = {}
    async with pool.acquire() as conn, conn.transaction():
        out["runs"] = await conn.execute(
            "DELETE FROM runs WHERE started_at < now() - make_interval(days => $1)", days
        )
        out["inbound"] = await conn.execute(
            "DELETE FROM inbound_messages WHERE received_at < now() - make_interval(days => $1)", days
        )
        out["asks"] = await conn.execute(
            "DELETE FROM claim_asks WHERE asked_at < now() - make_interval(days => $1)", days
        )
        # Follow-up pointers are only needed for the re-check window.
        out["pointers"] = await conn.execute(
            "UPDATE claim_asks SET chat_ref_enc = NULL WHERE chat_ref_enc IS NOT NULL AND asked_at < now() - interval '4 days'"
        )
        out["outbox"] = await conn.execute(
            "DELETE FROM outbox WHERE created_at < now() - make_interval(days => $1)", days
        )
        out["jobs"] = await conn.execute(
            "DELETE FROM jobs WHERE status IN ('done', 'dead') AND finished_at < now() - interval '7 days'"
        )
    return out
