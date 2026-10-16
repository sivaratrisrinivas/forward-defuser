# Data model

The SQL source of truth is `migrations/`. This page explains the shape and the privacy rules.

## In-memory objects (`src/defuser/models.py`)

- **CanonicalMessage**: the normalizer's output for any channel and media type. Fields: `channel`, `chat_ref`
  (opaque, encrypted at rest), `user_hash`, `message_id`, `text` (original text, caption, OCR text or transcript),
  `links` (fetched pages), `image` (bytes ref + phash + OCR + web-detection hits), `audio` (transcript, duration),
  `lang` (`en`, `hi`, `hinglish`, `ta`, ...), `forwarded`, `forwarded_many_times`, `is_group`.
- **TriageResult**: `claims[]` (each with `text` in the original language and `canonical_en`), `claim_type`
  (`health`, `politics`, `finance_scam`, `old_photo_new_context`, `deepfake`, `satire`, `opinion`, `other`),
  `checkable`, `lang`, `register` (`formal`, `casual`, `hinglish`), `injection_suspected`.
- **Evidence**: one fetched source. `evidence_id` (`E1`...), `url`, `final_url`, `publisher`, `tier`
  (`factchecker`, `official`, `news`, `other`, `satire`), `label`, `text` (the stored text quotes are checked against).
- **Draft**: what the LLM composes. `verdict`, `confidence`, `reasons[]` where each reason has `text` and
  `citations[] = {evidence_id, quote}`. Headline, next steps and helplines are not in the draft; they come from
  per-language templates.
- **VerifierReport**: `passed` plus a list of typed errors (`uncited_reason`, `unknown_evidence`, `quote_not_found`,
  `quote_too_short`, `verdict_inconsistent`, `instruction_leak`, `foreign_url`, `foreign_phone`, `number_not_in_quote`).

## Tables

| Table | Purpose | Retention |
|---|---|---|
| `users` | Hashed user id, language preference | Until `/forget` |
| `inbound_messages` | Dedupe key for webhooks (`channel, chat_hash, message_id`), job status, encrypted payload | Payload nulled after processing; rows purged after `RETENTION_DAYS` |
| `claims` | Claim clusters keyed by canonical English text + 256-d embedding (HNSW cosine index) | Kept (no personal data) |
| `claim_asks` | Who asked about which claim, encrypted chat pointer for follow-ups | Chat pointer nulled after the 72h follow-up window; rows purged after `RETENTION_DAYS` |
| `fingerprints` | Normalized-text sha256 and image 64-bit phash pointing at a claim | Kept (no personal data) |
| `runs` | One investigation: verdict, tokens, cost, latency, verifier result, trace summary | `RETENTION_DAYS` |
| `run_journal` | Ordered LLM responses and tool results so a killed run replays instead of re-executing | Deleted with the run |
| `evidence` | Sources fetched in a run, with the stored text quotes are verified against | Deleted with the run |
| `outbox` | Idempotent outbound messages (unique `idempotency_key`) | `RETENTION_DAYS` |
| `jobs` | Durable job queue with leases and idempotency keys | Finished jobs purged after 7 days |
| `review_items` | Human review queue for low-confidence and high-stakes verdicts | Until decided + `RETENTION_DAYS` |
| `eval_corrections` | Reviewer corrections, exported into `evals/` | Kept (claim text only) |
| `admin_alerts` | Viral-forward alerts, deduplicated per claim per day | Kept |

## Privacy rules enforced in code

- User and chat ids are stored only as `HMAC-SHA256(HASH_PEPPER, channel:id)`.
- The only reversible pointer to a person is `chat_ref_enc` (Fernet with `DATA_KEY`), needed to send replies
  and follow-ups. It is nulled when no longer needed.
- In groups the bot processes only `/check` commands (and replies to its own messages). Everything else is
  dropped in the webhook handler before anything is stored.
- `/forget` deletes `users`, `claim_asks`, `inbound_messages` and pending `outbox` rows for that user hash.
- A daily retention job deletes rows older than `RETENTION_DAYS` (default 30).
