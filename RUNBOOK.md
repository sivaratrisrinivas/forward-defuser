# Forward Defuser runbook

For whoever is on call. The README explains what the system is; this file is what to do when something is wrong.

## The system in one paragraph

`defuser-api` receives Telegram and WhatsApp webhooks, verifies them, drops anything the bot shouldn't read,
stores an encrypted copy of the message and queues a `process_inbound` job in Postgres, then answers 200. Workers
(`defuser-worker`) claim jobs with leases, run the check (triage, tool loop, composer, verifier), send the reply
through the idempotent outbox, and schedule follow-ups. Every model call and tool result is journaled per run, so a
retried job replays instead of paying again. Redis holds rate limits, daily spend counters and single-flight locks;
when Redis is down the system keeps working, with spend falling back to Postgres.

## Where to look

| What | Where |
|---|---|
| Dashboard (checks, p50/p95 latency, cost per check, verdict mix, cache hit rate, review queue) | `https://<api>/admin` (paste `ADMIN_TOKEN`) |
| Same numbers as JSON | `GET /admin/stats?hours=24` with `Authorization: Bearer $ADMIN_TOKEN` |
| Prometheus metrics | `GET /metrics` on the API, and on the worker's health port |
| Logs | Cloud Logging, JSON lines; filter `jsonPayload.logger="defuser.worker"` for job failures |
| Traces | OTLP to `OTEL_EXPORTER_OTLP_ENDPOINT`; one span per check with verdict, cache hit and language |
| A single run | `SELECT trace FROM runs WHERE id = '<run id>'` (triage, tool calls, investigator, draft, verifier) |

Metrics worth alerting on:

- `defuser_webhooks_total{outcome="bad_signature"}` rising: someone is probing the webhook, or a secret was
  rotated on one side only.
- Queue depth (dashboard "Queue / dead jobs", or `SELECT count(*) FROM jobs WHERE status='queued' AND run_at <= now()`)
  above a few hundred for more than 10 minutes: workers are behind.
- Any `dead` jobs.
- `defuser_budget_rejects_total{scope="global"}`: the global daily budget is exhausted and everyone gets
  "try again later".
- Verifier pass rate (dashboard) under 80%: the model is producing drafts the verifier rejects; check for a model
  or prompt change.

## Incidents

### Nobody is getting replies

1. `curl https://<api>/readyz`. If it fails, Postgres is unreachable from the API: check Cloud SQL status and the
   VPC connector. Telegram and Meta retry failed webhooks, and dedupe absorbs the repeats, so nothing is lost while
   the API returns errors.
2. Check the worker is running (`gcloud run services describe defuser-worker`) and its logs.
3. Queue growing but workers idle: look for a held lease (`SELECT id, kind, locked_by, locked_until FROM jobs WHERE
   status='running'`). Leases expire after 60 s and another worker takes the job.
4. Telegram only: `getWebhookInfo` (via `scripts/set_webhook.py --info`) shows `last_error_message`. A 403 there
   means `TELEGRAM_WEBHOOK_SECRET` differs between Secret Manager and the registered webhook: re-run
   `scripts/set_webhook.py`.

### Replies are slow

- p95 latency on the dashboard over about 30 s: compare queue depth with worker capacity. Capacity is roughly
  `instances × WORKER_CONCURRENCY` checks at a time, each taking the model's latency (10 to 25 s with tools).
  Raise `WORKER_CONCURRENCY` (keep `DB_POOL_SIZE` above it) or the worker's `--min-instances`.
- A slow outside API (search, fetch) shows up in traces as long tool spans. Tool calls have their own timeouts and
  the loop has a step cap, so a stuck API costs time, not correctness.

### The model API is down or overloaded

Checks fall back to UNVERIFIED ("I can't confirm this yet") and the run is marked for review. Nothing to do
during the outage. If it lasts, consider setting `LLM_REFUSAL_FALLBACK=1` only if the error is a refusal pattern,
not an outage. After recovery, UNVERIFIED claims get re-checked by the normal follow-up schedule.

### Cost spike

1. Dashboard "Total cost" and cost per check. A rise in cost per check usually means longer tool loops: look at
   `steps` in `runs` for the last hour.
2. A single user: `SELECT i.user_hash, sum(r.cost_usd) FROM runs r JOIN inbound_messages i ON i.id = r.inbound_id
   WHERE r.started_at > now() - interval '1 day' GROUP BY 1 ORDER BY 2 DESC LIMIT 10`. Per-user daily caps
   (`MAX_COST_PER_USER_DAY_USD`, `MAX_MESSAGES_PER_USER_DAY`) bound this already.
3. Emergency brake: lower `MAX_COST_GLOBAL_DAY_USD` (env var, redeploy the worker). Everyone gets the friendly
   "try again later" until the next UTC day.

### Dead jobs

```sql
SELECT id, kind, attempts, last_error FROM jobs WHERE status = 'dead' ORDER BY id DESC LIMIT 20;
-- after fixing the cause, re-drive them (replies are idempotent, so this can't double-send):
UPDATE jobs SET status = 'queued', attempts = 0, run_at = now(), last_error = NULL WHERE status = 'dead' AND kind = 'process_inbound';
```

A `process_inbound` job that fails on its last attempt already sent the person "something went wrong, please try
again", so re-driving it only matters for the records.

### Someone reports a wrong verdict

1. Find the run: the claim text is in `claims.canonical_text`; the run ids are in `runs` with that `claim_id`.
2. Read its `trace`: which evidence was saved, how it was labelled, what the verifier said.
3. If it is in the review queue, correct it there (`/admin`); otherwise insert a `review_items` row for the run
   and correct it. A correction takes the cached verdict out of service (the next ask re-investigates) and adds
   an eval case. Export with `uv run python scripts/export_corrections.py`, add frozen evidence to each case, and
   move it into `evals/golden.jsonl`.
4. A wrong FALSE or SCAM is the worst failure this system has. Treat it as an incident: find which rule let it
   through (verifier tier check, policy threshold) and add a test before changing anything.

### "Delete my data"

The person sends `/forget` (both channels). It deletes their follow-up subscriptions, their user row and any
stored message payloads. If they ask through another route, run
`uv run python scripts/forget_user.py telegram <their Telegram user id>` (or `whatsapp <phone number>`).
Retention runs daily (`RETENTION_DAYS`, default 30) and clears follow-up chat pointers after 4 days.

### Redis is down

Rate limiting fails open, daily spend falls back to summing today's runs in Postgres, and single-flight is skipped
(viral duplicates cost more until Redis is back). No action needed beyond restoring Redis.

### WhatsApp follow-ups not arriving

Free-form messages are only allowed within 24 hours of the person's last message. Outside that window Meta answers
error 131047; the bot logs it and gives up on that person without retrying. Sending late updates needs an approved
message template, which is not built.

## Routine tasks

- **Rotate `DATA_KEY`:** generate a key, set `DATA_KEY=<new>,<old>` (first encrypts, all decrypt), deploy both
  services, and drop the old key after `RETENTION_DAYS`. Do not rotate `HASH_PEPPER`: hashed ids would stop
  matching, which breaks rate limits and `/forget` for existing users.
- **Rotate `TELEGRAM_WEBHOOK_SECRET`:** update Secret Manager, deploy the API, then re-run `scripts/set_webhook.py`.
  Webhooks fail with 403 in between; Telegram retries them.
- **Change models:** `MODEL_MAIN` / `MODEL_CHEAP`. Run the live-model eval first
  (`uv run defuser-eval --mode live-model --gate evals/baseline.json`) and compare false-debunk rate and citation
  validity with the last run.
- **Add a language:** add `src/defuser/i18n/<code>.yaml` (copy `en.yaml`), list it in `SUPPORTED_LANGS`, add the
  script range to `normalize/text.py` if it has its own script, get a native speaker to review it, and add golden
  cases.
- **Load test before a launch:** `uv run python scripts/loadtest.py` against a scratch database (it wipes it).
