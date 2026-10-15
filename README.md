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

### M2 — images and voice notes

- Images: byte and pixel caps (decompression bombs rejected), downscale to 1600 px, 64-bit perceptual hash,
  then Google Vision text + web detection (or Claude vision for OCR when no Google key). Web detection backs the
  `reverse_image_search` tool, which fetches earlier appearances on fact-checker / news / official sites as
  evidence (the old-photo-new-context case).
- Voice notes: ffmpeg to 16 kHz mono FLAC, capped at 5 minutes (longer notes are cut and the reply says so),
  55-second chunks transcribed in parallel with Google Speech-to-Text.
- Five image/voice cases added to the golden set (160 total).
- **What broke:** the first perceptual-hash test compared two different drawings and failed; rewritten to compare
  a picture with its resized, re-compressed forward (distance ≤ 6) against a different picture (> 6).
- **Not verified here:** Vision `images:annotate` and Speech `v1p1beta1/speech:recognize` request/response shapes
  (Google docs were unreachable from the build sandbox); both are isolated in `media.py` and failures degrade to a
  text-only check.

### M3 — near-duplicate cache and cost controls

- Pre-triage cache: sha256 of normalised text, and image pHash within Hamming distance 6 (`bit_count` on
  `phash # x`), answer with zero model calls.
- Post-triage cache: the canonical English claim is embedded (local feature-hashing embedder, pgvector HNSW
  cosine index). A near match (≥ 0.75) is confirmed by the cheap model ("same checkable fact? different numbers,
  places, dates or a negation mean no") before its verdict is reused. A same-claim hit in another language
  re-runs only the composer (verified as usual), so the same rumour in five languages costs one investigation.
- Freshness: verified verdicts are reused for 30 days, UNVERIFIED for 6 hours; stale hits are re-investigated
  and the claim's verdict is updated. An UNVERIFIED result never overwrites a fresh verified one.
- Budgets: per-user per-minute rate limit at the webhook (one "slow down" notice, the rest dropped before
  storage), per-user daily messages and spend, global daily spend, per-message step and cost caps. Over budget
  gives a friendly "try again later". If Redis is down, daily spend falls back to summing today's runs in
  Postgres, so an outage never means unlimited spend.
- **What broke:** the first similarity thresholds (0.90 to reuse) almost never matched real paraphrases
  ("RBI is banning all 10 rupee coins" vs "RBI will ban 10 rupee coins" scores 0.66 on the hashing embedder),
  while "hot water kills coronavirus" vs "cold water kills coronavirus" would score high. Pure thresholds were
  wrong in both directions, so reuse now needs a cheap-model sameness confirmation on top of a lower candidate
  threshold.

### M4 — follow-ups, viral alerts, retention

- Every checked claim records who asked (hashed user id, language, verdict sent). Only UNVERIFIED answers keep an
  encrypted pointer to the chat, and retention clears it after 4 days; verified answers keep none.
- UNVERIFIED claims get re-check jobs at 6, 24 and 72 hours (`FOLLOWUP_HOURS`). When a re-check finds a verdict,
  everyone still waiting gets one "📢 Update" message in their own language (composed once per language, verified
  as usual). Keys `recheck:{claim}:{h}` and `followup:{claim}:{user}:{verdict}` make re-runs and duplicate jobs
  harmless; a still-UNVERIFIED re-check sends nothing.
- Viral alert: when `VIRAL_THRESHOLD_24H` people ask about one claim in 24 hours, one alert per claim per day
  goes to `ADMIN_TELEGRAM_CHAT_ID` and the admin_alerts table.
- `/forget` deletes the person's asks, so no follow-up reaches them. A daily `maintenance` job (seeded when a
  worker starts) applies `RETENTION_DAYS` to runs, inbound messages, asks, outbox and finished jobs.
- **What broke:** the re-check went through `cache.record`, which counts an ask, so every re-check inflated the
  claim's ask count and could trip the viral alert on its own; re-checks now take that count back. The first
  design also stored a chat pointer for every ask, which is more personal data than a verified answer needs.

### M5 — WhatsApp and more languages

- WhatsApp Cloud API channel: `GET /webhooks/whatsapp` answers Meta's verification handshake
  (`WHATSAPP_VERIFY_TOKEN`), `POST /webhooks/whatsapp` checks `X-Hub-Signature-256` (HMAC-SHA256 of the raw body
  with `WHATSAPP_APP_SECRET`) before parsing, then goes through the same rate limit, dedupe and queue as Telegram.
  Forwarded and "forwarded many times" flags come from `context`. Images and voice notes are downloaded through the
  media endpoint. Point the Meta app's webhook at `<PUBLIC_BASE_URL>/webhooks/whatsapp` and subscribe to `messages`.
- WhatsApp is 1:1 first ("forward it to the bot's number"). In groups (only business-created groups, a limited
  API) the bot acts on `/check <text>` only; a bare reply with `/check` gets an explanation, because the webhook
  carries only the replied message's id, not its content.
- Languages: templates for Tamil, Telugu, Bengali and Marathi added next to English, Hindi and Hinglish
  (`SUPPORTED_LANGS`). All non-English templates are unreviewed drafts (`reviewed: false`) and need a native
  speaker before launch. The scam helpline (1930, cybercrime.gov.in) is checked to be present in every language.
- **What broke:** follow-ups at 24 and 72 hours collide with WhatsApp's 24-hour customer-service window: a
  free-form message outside it fails with error 131047, and the job retried until it was dead-lettered. That error
  is now non-retryable: the person is marked as handled and the other subscribers still get their update. Sending
  late updates on WhatsApp needs an approved message template, which is not built.
- **Not verified here:** every WhatsApp Cloud API shape (developers.facebook.com was unreachable from the build
  sandbox). They were cross-checked against the open-source `pywa` client and are listed in
  `src/defuser/channels/whatsapp.py`; the Graph API version is configurable (`WHATSAPP_GRAPH_VERSION`).

## Eval results

Offline mode (frozen evidence, deterministic baseline policy instead of Claude; latency and cost are from stub
token counts, not real). Live-model numbers need an `ANTHROPIC_API_KEY` and run in CI when one is configured.

| Set | Cases | Accuracy | False-debunk rate | Citation validity | Abstention on unseen | Containment |
|---|---|---|---|---|---|---|
| Golden | 160 | 0.719 | 0.0 | 1.0 (130 citations) | 1.0 | n/a |
| Adversarial | 20 | 0.85 | 0.0 | 1.0 | n/a | 1.0 |
| Hijacked model (adversarial + 40 golden) | 60 | n/a | n/a | 1.0 | n/a | 1.0 |

The baseline mostly misses non-English variants (it matches words in English only), which is exactly what the
model's `canonical_en` claim fixes; its misses are abstentions, not wrong debunks.
