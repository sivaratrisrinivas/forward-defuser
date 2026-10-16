from defuser.channels.telegram import parse_update
from defuser.ingest import ingest_event
from defuser.jobs import queue
from tests.conftest import drain_jobs
from tests.test_m0_telegram import upd


async def test_duplicate_webhook_creates_one_job_and_one_reply(clean, tg, monkeypatch):
    import defuser.pipeline as pipeline

    async def echo(pool, inbound_id):
        from defuser.models import ChatRef
        from defuser.outbox import send_once
        from defuser.privacy import decrypt_json

        row = await pool.fetchrow("SELECT payload_enc FROM inbound_messages WHERE id=$1", inbound_id)
        data = decrypt_json(row["payload_enc"])
        await send_once(pool, f"reply:{inbound_id}", ChatRef(**data["chat"]), "echo " + data["event"]["text"])

    monkeypatch.setattr(pipeline, "process_inbound_message", echo)
    ev = parse_update(upd("hello"), "DefuserBot")
    first = await ingest_event(clean, ev)
    second = await ingest_event(clean, ev)
    assert first and second is None
    assert await clean.fetchval("SELECT count(*) FROM jobs") == 1
    await drain_jobs(clean)
    # Job re-run (e.g. resumed after crash) must not re-send.
    await queue.enqueue(clean, "process_inbound", {"inbound_id": first}, "rerun")
    await drain_jobs(clean)
    assert len(tg.sent) == 1 and tg.sent[0][1] == "echo hello"


async def test_lease_expiry_lets_another_worker_resume(clean):
    await queue.enqueue(clean, "noop", {}, "k1")
    j = await queue.claim(clean, "w1", lease_s=60)
    assert j and await queue.claim(clean, "w2", lease_s=60) is None
    await clean.execute("UPDATE jobs SET locked_until = now() - interval '1 second'")  # w1 died
    j2 = await queue.claim(clean, "w2", lease_s=60)
    assert j2 and j2.id == j.id and j2.attempts == 2
    await queue.complete(clean, j.id, "w1")  # stale worker cannot complete
    assert await clean.fetchval("SELECT status FROM jobs") == "running"


async def test_failed_send_retries_then_sends_once(clean, tg):
    from defuser.models import ChatRef
    from defuser.outbox import send_once

    tg.fail_next = 1
    chat = ChatRef(channel="telegram", chat_id="1")
    try:
        await send_once(clean, "k", chat, "hi")
    except RuntimeError:
        pass
    await send_once(clean, "k", chat, "hi")
    await send_once(clean, "k", chat, "hi")
    assert len(tg.sent) == 1


async def test_dead_letter_after_max_attempts(clean):
    await queue.enqueue(clean, "boom", {}, "k", max_attempts=1)
    j = await queue.claim(clean, "w", 60)
    await queue.fail(clean, j, "w", "x")
    assert await clean.fetchval("SELECT status FROM jobs") == "dead"
