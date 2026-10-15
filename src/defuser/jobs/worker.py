"""Worker process: claims jobs and dispatches them to handlers. Run with `defuser-worker`."""

import asyncio
import logging
import os
import signal
import socket
import uuid
from collections.abc import Awaitable, Callable

from defuser.config import get_settings
from defuser.db import get_pool
from defuser.jobs import queue

log = logging.getLogger("defuser.worker")

Handler = Callable[[dict, queue.Job], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}

LEASE_S = 60


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


async def _run_one(pool, job: queue.Job, worker_id: str) -> None:
    fn = HANDLERS.get(job.kind)
    if fn is None:
        await queue.fail(pool, job, worker_id, f"no handler for {job.kind}")
        return

    async def keep_lease():
        while True:
            await asyncio.sleep(LEASE_S / 3)
            await queue.heartbeat(pool, job.id, worker_id, LEASE_S)

    hb = asyncio.create_task(keep_lease())
    try:
        await fn(job.payload, job)
        await queue.complete(pool, job.id, worker_id)
    except Exception as e:  # noqa: BLE001 - every failure must be recorded on the job
        log.exception("job %s (%s) failed", job.id, job.kind)
        await queue.fail(pool, job, worker_id, f"{type(e).__name__}: {e}")
    finally:
        hb.cancel()


async def run_worker(stop: asyncio.Event, concurrency: int | None = None, idle_sleep: float = 0.5) -> None:
    import defuser.handlers  # noqa: F401 - registers handlers

    pool = await get_pool()
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    sem = asyncio.Semaphore(concurrency or get_settings().worker_concurrency)
    tasks: set[asyncio.Task] = set()
    log.info("worker %s started", worker_id)
    while not stop.is_set():
        await sem.acquire()
        job = await queue.claim(pool, worker_id, LEASE_S)
        if job is None:
            sem.release()
            try:
                await asyncio.wait_for(stop.wait(), timeout=idle_sleep)
            except TimeoutError:
                pass
            continue

        async def go(j=job):
            try:
                await _run_one(pool, j, worker_id)
            finally:
                sem.release()

        t = asyncio.create_task(go())
        tasks.add(t)
        t.add_done_callback(tasks.discard)
    # Graceful shutdown: let in-flight jobs finish; anything killed harder is recovered by lease expiry.
    if tasks:
        await asyncio.wait(tasks, timeout=25)


def main() -> None:
    from defuser.observability import setup_logging, setup_tracing

    setup_logging()
    setup_tracing("defuser-worker")
    stop = asyncio.Event()

    async def _main():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        from defuser.health import start_worker_health_server

        await start_worker_health_server()
        await run_worker(stop)

    asyncio.run(_main())
