"""FastAPI app: webhooks, health, metrics, admin."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from defuser.channels import telegram
from defuser.config import get_settings
from defuser.db import close_pool, get_pool
from defuser.ingest import ingest_event
from defuser.observability import WEBHOOKS, setup_logging, setup_tracing

log = logging.getLogger("defuser.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    setup_tracing("defuser-api")
    await get_pool()
    worker_task = None
    import os

    if os.environ.get("RUN_WORKER_IN_PROCESS") == "1":
        from defuser.jobs.worker import run_worker

        stop = asyncio.Event()
        worker_task = asyncio.create_task(run_worker(stop))
    yield
    if worker_task:
        stop.set()
        await worker_task
    await close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="Forward Defuser", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/readyz")
    async def readyz():
        pool = await get_pool()
        await pool.fetchval("SELECT 1")
        return {"ok": True}

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/webhooks/telegram")
    async def telegram_webhook(request: Request):
        s = get_settings()
        expected = s.telegram_webhook_secret.get_secret_value() if s.telegram_webhook_secret else None
        if not telegram.verify_secret(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), expected):
            WEBHOOKS.labels("telegram", "bad_signature").inc()
            return PlainTextResponse("forbidden", status_code=403)
        try:
            update = await request.json()
        except ValueError:
            WEBHOOKS.labels("telegram", "bad_json").inc()
            return JSONResponse({"ok": True})  # never make Telegram retry garbage
        ev = telegram.parse_update(update, s.telegram_bot_username)
        if ev is None:
            WEBHOOKS.labels("telegram", "ignored").inc()
            return JSONResponse({"ok": True})
        inbound_id = await ingest_event(await get_pool(), ev)
        WEBHOOKS.labels("telegram", "queued" if inbound_id else "duplicate").inc()
        return JSONResponse({"ok": True})

    from defuser.admin.routes import router as admin_router

    app.include_router(admin_router)
    return app


app = create_app()
