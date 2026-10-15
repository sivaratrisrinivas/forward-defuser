# Plan

## Goal

A person forwards a dodgy message to the bot (or replies `/check` to one in a family group). Within
about 20 seconds the bot replies in the sender's language with a one-line verdict, two or three
cited reasons, source links and a next step. Claims it cannot verify get `UNVERIFIED`, and the bot
re-checks them at 6h, 24h and 72h and tells the asker if the verdict changes.

## Decisions (and why)

| Area | Choice | Why |
|---|---|---|
| Durable workflows | Postgres-backed jobs (`jobs` table, `FOR UPDATE SKIP LOCKED`, leases, idempotency keys) plus a per-run **journal** of LLM and tool steps | One less service than Temporal. The journal gives Temporal-style replay: a worker killed mid-investigation resumes from the last persisted step without re-paying for LLM calls or re-sending replies. |
| Language | Python 3.12, FastAPI, asyncpg, redis-py | As specified. |
| LLM | One `LLMProvider` interface; Anthropic implementation; `MODEL_MAIN` / `MODEL_CHEAP` from env | Models swap by config. Fakes implement the same interface for tests and offline evals. |
| Structured output | `output_config.format` JSON schema for triage and drafting; strict tools for the agent | Current Claude models reject forced `tool_choice`, so the loop uses `auto` plus a `finish_investigation` tool and re-prompts if the model stops without calling it. |
| Embeddings | Pluggable `Embedder`; default is a local character n-gram hashing embedder (256-d) | No extra vendor needed to run. Cross-language dedupe works because the cache key is the triage model's **canonical English claim**, not the raw text. A hosted multilingual embedder can be plugged in later. |
| Reply text | LLM writes only the reasons (each with citations); headline, next steps, helplines and humour lines come from per-language config templates | Keeps every factual sentence inside the verifier's reach and makes new languages a config change. |
| Group trigger on Telegram | Privacy mode ON; users reply `/check` to a message. DMs and forwards to the bot are processed directly | Telegram's docs say privacy-mode bots only receive commands and replies to the bot in groups, so the bot never sees ordinary group chatter. |
| WhatsApp | 1:1 "forward it to the bot's number" flow | Meta's Groups API is limited to Official Business Accounts and invite-only groups of up to 8 people created by the business, so the bot cannot join existing family groups. |
| Deployment | Docker image; Cloud Run (default) with Cloud SQL Postgres and Memorystore Redis; Fly.io config also provided | The Fact Check, Vision, Speech and Safe Browsing APIs are already on GCP. |

## Request flow

```
webhook ─▶ verify signature ─▶ dedupe (inbound_messages unique key) ─▶ enqueue job ─▶ 200 OK
                                                                         │
worker ─▶ budget check ─▶ normalize (text/link/image/voice) ─▶ exact/phash cache ─▶ triage (cheap model)
      ─▶ claim cache (embedding of canonical claim) ─▶ investigation tool loop (main model, journaled)
      ─▶ compose draft (cited reasons) ─▶ deterministic verifier ─(fail)▶ regenerate once ─(fail)▶ UNVERIFIED
      ─▶ render in sender's language ─▶ outbox (idempotent send) ─▶ schedule follow-ups / alerts
```

## Milestones

- **M0** Repo, Docker, CI, Telegram echo bot, Cloud Run config, secrets via env / Secret Manager.
- **M1** Text claim check: normalizer (text + links), triage, tool loop, verifier, abstention, composer, eval harness.
- **M2** Images (OCR, reverse image search via Vision web detection, phash) and voice notes (ffmpeg + ASR).
- **M3** Near-duplicate cache (claim embeddings + image phash) and budgets (per message, per user/day, global/day, rate limit).
- **M4** Durable follow-ups (6h/24h/72h), claim clustering, viral alerts, scam reporting guidance, retention and delete-my-data.
- **M5** WhatsApp Cloud API adapter; multilingual replies by config (en, hi, Hinglish; ta, te, bn, mr scaffolded).
- **M6** Chaos tests, review queue, dashboards, OpenTelemetry, runbook, load test.
