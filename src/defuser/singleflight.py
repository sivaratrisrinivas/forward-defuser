"""Single-flight for viral forwards: when many people forward the same message at once, one job investigates and
the others wait for its cached answer instead of paying for the same investigation in parallel.

Redis `SET NX PX` lock keyed by the message fingerprint. If Redis is down, everyone proceeds (costs more, never
blocks). A waiter gives up after `wait_s` and investigates itself, so a crashed lock holder costs latency, not
answers."""

import asyncio
import logging
import uuid

from defuser.budget import get_redis
from defuser.embeddings import text_fingerprint
from defuser.models import CanonicalMessage

log = logging.getLogger(__name__)
NO_LOCK = "no-lock"
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def flight_key(msg: CanonicalMessage) -> str | None:
    text = msg.combined_text()
    if len(text) >= 20:
        return f"sf:t:{text_fingerprint(text)}"
    if msg.image and msg.image.phash:
        return f"sf:i:{msg.image.phash}"
    return None


async def acquire(key: str, ttl_s: int = 180) -> str | None:
    """A token when we own the work (or Redis is unavailable), None when someone else does."""
    token = uuid.uuid4().hex
    try:
        ok = await get_redis().set(key, token, nx=True, px=ttl_s * 1000)
    except Exception as e:  # noqa: BLE001
        log.warning("single-flight unavailable: %s", e)
        return NO_LOCK
    return token if ok else None


async def release(key: str, token: str | None) -> None:
    if not token or token == NO_LOCK:
        return
    try:
        await get_redis().eval(_RELEASE, 1, key, token)
    except Exception:  # noqa: BLE001 - the TTL cleans up
        pass


async def wait_for(key: str, check, wait_s: float = 90, poll_s: float = 0.5):
    """Poll `check()` until it returns something, the lock is released, or time runs out."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while loop.time() < deadline:
        found = await check()
        if found is not None:
            return found
        try:
            if not await get_redis().exists(key):
                return await check()
        except Exception:  # noqa: BLE001
            return None
        await asyncio.sleep(poll_s)
    return None
