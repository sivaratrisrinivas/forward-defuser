"""In-process load test: webhook -> queue -> workers -> reply, against real Postgres and Redis.

The model and the outside world are simulated (BaselinePolicyLLM with an artificial delay per call, frozen
fact-check/search/page fixtures), so this measures the system around the model: webhook ack latency, queueing,
DB and Redis contention, cache behaviour on viral duplicates, retries, and exactly-once replies. Model latency
and cost in production come from the live-model eval and the dashboard, not from here.

    DATABASE_URL=postgresql://fd:fd@localhost:5432/fd_load uv run python scripts/loadtest.py \
        --messages 600 --rps 40 --workers 4 --concurrency 8 --llm-delay 0.4 --dup-rate 0.4 --llm-error-rate 0.05

The target database is wiped first: never point it at production.
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://fd:fd@localhost:5432/fd_load")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/14")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "loadtest")
os.environ.setdefault("TELEGRAM_BOT_USERNAME", "DefuserBot")
os.environ.setdefault("HASH_PEPPER", "loadtest")
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("MAX_MESSAGES_PER_USER_MINUTE", "1000")
os.environ.setdefault("MAX_MESSAGES_PER_USER_DAY", "100000")
os.environ.setdefault("MAX_COST_GLOBAL_DAY_USD", "100000")
os.environ.setdefault("MAX_COST_PER_USER_DAY_USD", "100000")
if "DATA_KEY" not in os.environ:
    from cryptography.fernet import Fernet

    os.environ["DATA_KEY"] = Fernet.generate_key().decode()

import asyncpg  # noqa: E402
import httpx  # noqa: E402

from defuser import db, pipeline  # noqa: E402
from defuser.channels import registry  # noqa: E402
from defuser.evals.baseline_llm import BaselinePolicyLLM  # noqa: E402
from defuser.evals.fixtures import backends_from_case  # noqa: E402
from defuser.jobs import worker  # noqa: E402
from defuser.llm.provider import LLMError, set_llm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class SimLLM:
    def __init__(self, delay: float, error_rate: float):
        self.inner, self.delay, self.error_rate, self.calls = BaselinePolicyLLM(), delay, error_rate, 0

    async def complete(self, **kw):
        self.calls += 1
        await asyncio.sleep(self.delay * random.uniform(0.5, 1.5))
        if random.random() < self.error_rate:
            raise LLMError("simulated 529 overloaded")
        return await self.inner.complete(**kw)


class RecordingChannel:
    name = "telegram"

    def __init__(self, send_delay: float):
        self.sent: dict[str, list[float]] = {}
        self.send_delay = send_delay

    async def send_text(self, chat_id, text, reply_to=None):
        await asyncio.sleep(self.send_delay)
        self.sent.setdefault(f"{chat_id}:{reply_to}", []).append(time.monotonic())
        return str(sum(len(v) for v in self.sent.values()))

    async def typing(self, chat_id, message_id=None):
        return None

    async def download(self, media):
        raise RuntimeError("no media in load test")


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


async def main(a) -> dict:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    await db.migrate(os.environ["DATABASE_URL"])
    pool = await db.get_pool()
    import redis.asyncio as aioredis

    r = aioredis.from_url(os.environ["REDIS_URL"])
    await r.flushdb()
    await r.aclose()

    cases = [json.loads(x) for x in (ROOT / "evals/golden.jsonl").read_text().splitlines() if x.strip()]
    cases = [c for c in cases if not c.get("image") and not c.get("audio")]
    base = random.Random(7).choice(cases)
    backends, fetcher = backends_from_case(base["fixtures"])
    pipeline.set_deps(backends=backends, fetcher=fetcher)
    llm = SimLLM(a.llm_delay, a.llm_error_rate)
    set_llm(llm)
    ch = RecordingChannel(a.send_delay)
    registry.set_channel("telegram", ch)
    worker.LEASE_S = 15

    rng = random.Random(42)
    viral = [c["message"] for c in rng.sample(cases, 5)]
    plan = []
    for i in range(a.messages):
        text = rng.choice(viral) if rng.random() < a.dup_rate else rng.choice(cases)["message"] + f" #{i}"
        plan.append((i, text))

    from defuser.app import create_app

    app = create_app()
    stops = [asyncio.Event() for _ in range(a.workers)]
    workers = [
        asyncio.create_task(worker.run_worker(s, concurrency=a.concurrency, idle_sleep=0.05)) for s in stops
    ]
    acks, sent_at = [], {}
    t0 = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lt") as c:

        async def post(i, text):
            upd = {
                "update_id": i,
                "message": {
                    "message_id": i,
                    "chat": {"id": 10_000 + i % 400, "type": "private"},
                    "from": {"id": 10_000 + i % 400, "language_code": "en"},
                    "text": text,
                    "forward_origin": {"type": "hidden_user", "sender_user_name": "x", "date": 0},
                },
            }
            s = time.monotonic()
            res = await c.post(
                "/webhooks/telegram", json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "loadtest"}
            )
            acks.append((time.monotonic() - s) * 1000)
            sent_at[f"{10_000 + i % 400}:{i}"] = s
            if res.status_code != 200:
                print("webhook error", res.status_code)
            if rng.random() < a.redeliver_rate:  # Telegram redelivery: must not produce a second reply
                await c.post(
                    "/webhooks/telegram", json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "loadtest"}
                )

        tasks = []
        for i, text in plan:
            tasks.append(asyncio.create_task(post(i, text)))
            await asyncio.sleep(1 / a.rps)
        await asyncio.gather(*tasks)
        ingest_done = time.monotonic() - t0
        deadline = time.monotonic() + a.timeout
        while time.monotonic() < deadline:
            pending = await pool.fetchval(
                "SELECT count(*) FROM jobs WHERE kind = 'process_inbound' AND status IN ('queued', 'running')"
            )
            if pending == 0:
                break
            await asyncio.sleep(0.25)
    total = time.monotonic() - t0
    for s in stops:
        s.set()
    await asyncio.gather(*workers, return_exceptions=True)

    e2e = [(v[0] - sent_at[k]) * 1000 for k, v in ch.sent.items() if k in sent_at]
    dupes = sum(1 for v in ch.sent.values() if len(v) > 1)
    jobs = {r["status"]: r["n"] for r in await pool.fetch("SELECT status, count(*) n FROM jobs GROUP BY 1")}
    hits = {
        r["h"]: r["n"]
        for r in await pool.fetch("SELECT COALESCE(cache_hit,'none') h, count(*) n FROM runs GROUP BY 1")
    }
    verdicts = {
        r["verdict"]: r["n"] for r in await pool.fetch("SELECT verdict, count(*) n FROM runs GROUP BY 1")
    }
    report = {
        "config": vars(a),
        "messages": a.messages,
        "answered": len(ch.sent),
        "duplicate_replies": dupes,
        "unanswered": a.messages - len(ch.sent),
        "ingest_seconds": round(ingest_done, 1),
        "total_seconds": round(total, 1),
        "throughput_msgs_per_s": round(len(ch.sent) / total, 1),
        "webhook_ack_ms": {
            "p50": round(pct(acks, 50), 1),
            "p95": round(pct(acks, 95), 1),
            "p99": round(pct(acks, 99), 1),
            "max": round(max(acks), 1),
        },
        "end_to_end_ms": {
            "p50": round(pct(e2e, 50)),
            "p95": round(pct(e2e, 95)),
            "p99": round(pct(e2e, 99)),
            "mean": round(statistics.mean(e2e)) if e2e else 0,
        },
        "llm_calls": llm.calls,
        "jobs": jobs,
        "cache_hits": hits,
        "verdicts": verdicts,
    }
    await db.close_pool()
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=400)
    ap.add_argument("--rps", type=float, default=30)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--llm-delay", type=float, default=0.3)
    ap.add_argument("--llm-error-rate", type=float, default=0.0)
    ap.add_argument("--send-delay", type=float, default=0.05)
    ap.add_argument("--dup-rate", type=float, default=0.4)
    ap.add_argument("--redeliver-rate", type=float, default=0.05)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--out", default="loadtest-report.json")
    ap.add_argument("--check", action="store_true", help="exit 1 unless every message got exactly one reply")
    a = ap.parse_args()
    import logging

    logging.basicConfig(level=logging.ERROR)
    rep = asyncio.run(main(a))
    Path(a.out).write_text(json.dumps(rep, indent=2))
    print(json.dumps(rep, indent=2))
    if a.check and (rep["unanswered"] or rep["duplicate_replies"] or rep["jobs"].get("dead")):
        raise SystemExit("load test failed: unanswered, duplicate or dead messages")
