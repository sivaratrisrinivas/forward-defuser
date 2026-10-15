import asyncio
import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "postgresql://fd:fd@localhost:5432/fd_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("DATA_KEY", Fernet.generate_key().decode())
os.environ.setdefault("HASH_PEPPER", "test-pepper")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "s3cret")
os.environ.setdefault("TELEGRAM_BOT_USERNAME", "DefuserBot")
os.environ.setdefault("ADMIN_TOKEN", "admintoken")
os.environ.setdefault("LLM_PROVIDER", "fake")

import asyncpg  # noqa: E402

from defuser import db  # noqa: E402
from defuser.channels import registry  # noqa: E402


class FakeChannel:
    def __init__(self, name="telegram"):
        self.name = name
        self.sent: list[tuple[str, str, str | None]] = []
        self.fail_next = 0
        self.media: dict[str, bytes] = {}

    async def send_text(self, chat_id, text, reply_to=None):
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("channel down")
        self.sent.append((chat_id, text, reply_to))
        return str(len(self.sent))

    async def typing(self, chat_id, message_id=None):
        return None

    async def download(self, media):
        return self.media[media.file_id]


@pytest.fixture(scope="session")
async def pool():
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    await db.migrate(dsn)
    p = await db.get_pool(dsn)
    yield p
    await db.close_pool()


@pytest.fixture
async def clean(pool):
    await pool.execute(
        "TRUNCATE inbound_messages, users, claims, claim_asks, fingerprints, runs, run_journal, evidence, outbox, jobs, "
        "review_items, eval_corrections, admin_alerts RESTART IDENTITY CASCADE"
    )
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(os.environ["REDIS_URL"])
        await r.flushdb()
        await r.aclose()
    except Exception:
        pass
    yield pool


@pytest.fixture
def tg():
    ch = FakeChannel("telegram")
    registry.set_channel("telegram", ch)
    yield ch
    registry.set_channel("telegram", None)


async def drain_jobs(pool, max_jobs=50):
    """Run queued jobs inline until none are ready."""
    import defuser.handlers  # noqa: F401
    from defuser.jobs import queue
    from defuser.jobs.worker import _run_one

    n = 0
    while n < max_jobs:
        job = await queue.claim(pool, "test-worker", 60)
        if job is None:
            break
        await _run_one(pool, job, "test-worker")
        n += 1
    await asyncio.sleep(0)
    return n
