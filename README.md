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

### M1 — text claim check, tool loop, verifier, abstention, evals

- Triage (cheap model, structured output) extracts up to 3 claims with a canonical English form, the type and
  the language/register, and flags prompt-injection attempts.
- Investigation (main model) is a tool loop over `search_factchecks`, `web_search`, `fetch_url`,
  `lookup_official_source`, `check_url_safety`, `save_evidence`, `schedule_followup` and `finish_investigation`,
  bounded by step and cost caps. Every LLM response and tool result is written to `run_journal`, so a killed
  worker resumes by replaying instead of re-paying.
- Source tiers (fact-checker / official / news / satire / other) come from a domain list, never from the model,
  so lookalike domains (`pib.gov.in.press-release.co`, `altnews-in.com`) stay "other".
- The composer writes only the reasons, each with `{evidence_id, quote}` citations. The deterministic verifier
  checks quotes are verbatim in stored source text, numbers in reasons appear in quotes, no URLs / phone numbers
  / instruction-like text leak, and the verdict is backed by labelled evidence of a strong enough tier (FALSE and
  SCAM need a fact-checker, official source or Safe Browsing hit; TRUE is blocked if any fact-check record for the
  claim says false). One regeneration, then UNVERIFIED. FALSE/SCAM under 0.75 confidence are downgraded and sent
  to the review queue.
- Headline, next steps and helplines come from per-language templates (`src/defuser/i18n/*.yaml`).
- **What broke:** (1) YAML read the template keys `TRUE:`/`FALSE:` as booleans, so the FALSE headline lookup
  failed; the loader now normalises boolean keys. (2) Fact-check index records were registered under the article
  URL, so the later `fetch_url` of that article was skipped as "already fetched" and the composer cited an id
  that never existed; only real page fetches are deduplicated now. (3) The first offline eval found the
  baseline labelling "You do not need to enter your UPI PIN to receive money" as a refutation and answering FALSE
  to a TRUE claim, and FALSE→TRUE on a plastic-rice variant. The verifier now blocks TRUE whenever any fact-check
  record for the claim carries a false/misleading rating (and FALSE when one says true), and rejects a "supports"
  label on a record rated false.

## Eval results

Offline mode (frozen evidence, deterministic baseline policy instead of Claude; latency and cost are from stub
token counts, not real). Live-model numbers need an `ANTHROPIC_API_KEY` and run in CI when one is configured.

| Set | Cases | Accuracy | False-debunk rate | Citation validity | Abstention on unseen | Containment |
|---|---|---|---|---|---|---|
| Golden | 155 | 0.729 | 0.0 | 1.0 (127 citations) | 1.0 | n/a |
| Adversarial | 20 | 0.85 | 0.0 | 1.0 | n/a | 1.0 |
| Hijacked model (adversarial + 40 golden) | 60 | n/a | n/a | 1.0 | n/a | 1.0 |

The baseline mostly misses non-English variants (it matches words in English only), which is exactly what the
model's `canonical_en` claim fixes; its misses are abstentions, not wrong debunks.
