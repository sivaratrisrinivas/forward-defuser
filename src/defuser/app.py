"""FastAPI app: webhooks, health, metrics, admin."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from defuser.budget import check_rate
from defuser.channels import telegram, whatsapp
from defuser.config import get_settings
from defuser.db import close_pool, get_pool
from defuser.ingest import ingest_event
from defuser.observability import WEBHOOKS, setup_logging, setup_tracing
from defuser.privacy import hash_id

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
        await _admit(ev, "telegram")
        return JSONResponse({"ok": True})

    async def _admit(ev, channel_name: str) -> None:
        allowance = await check_rate(hash_id(ev.channel, ev.user_id))
        if not allowance.ok:
            if not allowance.notify:
                WEBHOOKS.labels(channel_name, "rate_limited").inc()
                return
            ev = ev.model_copy(update={"command": "rate_limited", "text": "", "media": None})
        inbound_id = await ingest_event(await get_pool(), ev)
        WEBHOOKS.labels(channel_name, "queued" if inbound_id else "duplicate").inc()

    @app.get("/webhooks/whatsapp")
    async def whatsapp_verify(request: Request):
        s = get_settings()
        token = s.whatsapp_verify_token.get_secret_value() if s.whatsapp_verify_token else None
        challenge = whatsapp.verify_handshake(dict(request.query_params), token)
        if challenge is None:
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse(challenge)

    @app.post("/webhooks/whatsapp")
    async def whatsapp_webhook(request: Request):
        s = get_settings()
        raw = await request.body()
        secret = s.whatsapp_app_secret.get_secret_value() if s.whatsapp_app_secret else None
        if not whatsapp.verify_signature(raw, request.headers.get("X-Hub-Signature-256"), secret):
            WEBHOOKS.labels("whatsapp", "bad_signature").inc()
            return PlainTextResponse("forbidden", status_code=403)
        try:
            import json

            payload = json.loads(raw)
        except ValueError:
            WEBHOOKS.labels("whatsapp", "bad_json").inc()
            return JSONResponse({"ok": True})
        events = whatsapp.parse_webhook(payload)
        if not events:
            WEBHOOKS.labels("whatsapp", "ignored").inc()  # status callbacks, group chatter, unsupported types
        for ev in events:
            await _admit(ev, "whatsapp")
        return JSONResponse({"ok": True})

    from defuser.admin.routes import router as admin_router

    app.include_router(admin_router)
    return app


app = create_app()
