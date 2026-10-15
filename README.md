# Forward Defuser

A Telegram (and WhatsApp) bot that fact-checks forwarded messages — chain forwards, health rumours, political
claims, scam links — and replies in the sender's language with a verdict, short cited reasons, source links and
what to do next. Built for Indian families: English, Hindi and Hinglish at launch, more languages by config.

See [docs/PLAN.md](docs/PLAN.md) for the plan and design decisions and [docs/DATA_MODEL.md](docs/DATA_MODEL.md)
for the data model.

## Quick start (local)

```bash
cp .env.example .env
uv run python scripts/gen_keys.py >> .env      # DATA_KEY, HASH_PEPPER, webhook secret, admin token
docker compose up -d db redis minio
uv sync
uv run defuser-migrate
uv run uvicorn defuser.app:app --port 8080     # API (webhooks)
uv run defuser-worker                          # job worker
```

Tests need Postgres with pgvector and Redis (`docker compose up -d db redis`, then create a `fd_test` database):

```bash
uv run pytest -q
```

## Deploy (Cloud Run)

1. Create a GCP project, a Cloud SQL Postgres 16 instance (enable the `vector` extension, which Cloud SQL
   supports), a Redis instance (Memorystore) reachable through Direct VPC egress, and an Artifact Registry repo.
2. Create the Secret Manager secrets named in `deploy/cloudrun/deploy.sh` (values from `.env.example`).
3. `PROJECT=... SQL_INSTANCE=project:region:instance TELEGRAM_BOT_USERNAME=... ./deploy/cloudrun/deploy.sh`
4. Register the webhook: `PUBLIC_BASE_URL=<api url> uv run python scripts/set_webhook.py`

Two services are deployed from one image: `defuser-api` (webhooks, admin, metrics) and `defuser-worker`
(job worker, CPU always allocated). `ROLE=all` runs both in one process for small deployments.
A Fly.io config is in `deploy/fly/fly.toml`.

## Milestone notes

### M0 — repo, Docker, CI, Telegram webhook

- Webhook verifies `X-Telegram-Bot-Api-Secret-Token`, drops anything in a group that isn't a `/check`
  command, dedupes on `(channel, chat, message_id)`, enqueues a durable job and returns 200 immediately.
- Postgres job queue with leases (`FOR UPDATE SKIP LOCKED`), heartbeats, exponential backoff, dead-lettering.
- Idempotent outbox so re-run jobs never re-send a reply.
- **What broke:** the first Docker build pulled `uv` from ghcr.io, which the build sandbox could not reach;
  switched to `pip install uv`. FastAPI's `TestClient` runs its own event loop and closed the shared asyncpg
  pool from the wrong loop; tests now use `httpx.ASGITransport` on the test loop.
- **Not verified here:** the Telegram cloud Bot API file download limit (commonly 20 MB) and the file download
  URL format, because core.telegram.org's getFile section could not be fetched from the build sandbox.
