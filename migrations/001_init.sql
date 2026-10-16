-- Forward Defuser schema. See docs/DATA_MODEL.md for the reasoning behind each table.
CREATE EXTENSION IF NOT EXISTS vector;

-- People are only ever stored as keyed hashes (HMAC-SHA256 with HASH_PEPPER).
CREATE TABLE users (
    user_hash      TEXT PRIMARY KEY,
    channel        TEXT NOT NULL,
    lang_pref      TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every webhook delivery lands here first. The unique key is what makes duplicate webhooks harmless.
CREATE TABLE inbound_messages (
    id             BIGSERIAL PRIMARY KEY,
    channel        TEXT NOT NULL,
    chat_hash      TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    user_hash      TEXT NOT NULL,
    kind           TEXT NOT NULL,              -- text | image | voice | link | command
    status         TEXT NOT NULL DEFAULT 'queued', -- queued | processing | done | ignored | failed
    payload_enc    BYTEA,                      -- encrypted normalized request; purged after processing
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (channel, chat_hash, message_id)
);
CREATE INDEX inbound_user_idx ON inbound_messages (user_hash);

-- A claim cluster: one canonical English claim that many forwards (in many languages) map to.
CREATE TABLE claims (
    id               BIGSERIAL PRIMARY KEY,
    canonical_text   TEXT NOT NULL,
    claim_type       TEXT NOT NULL,
    embedding        vector(256) NOT NULL,
    verdict          TEXT NOT NULL DEFAULT 'UNVERIFIED',
    confidence       REAL NOT NULL DEFAULT 0,
    verdict_run_id   UUID,
    verdict_at       TIMESTAMPTZ,
    checked_at       TIMESTAMPTZ,
    ask_count        INTEGER NOT NULL DEFAULT 0,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT verdict_valid CHECK (verdict IN ('FALSE','MISLEADING','TRUE','SATIRE','SCAM','UNVERIFIED'))
);
CREATE INDEX claims_embedding_idx ON claims USING hnsw (embedding vector_cosine_ops);

-- Who asked about a claim, so follow-ups reach them. chat_ref_enc is the only reversible pointer to a person
-- and is deleted once the follow-up window closes (or on /forget).
CREATE TABLE claim_asks (
    id                 BIGSERIAL PRIMARY KEY,
    claim_id           BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    user_hash          TEXT NOT NULL,
    channel            TEXT NOT NULL,
    chat_ref_enc       BYTEA,
    lang               TEXT NOT NULL,
    verdict_sent       TEXT NOT NULL,
    asked_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, user_hash)
);
CREATE INDEX claim_asks_user_idx ON claim_asks (user_hash);
CREATE INDEX claim_asks_time_idx ON claim_asks (claim_id, asked_at);

-- Cheap pre-triage cache keys: normalized-text sha256 and 64-bit image perceptual hashes.
CREATE TABLE fingerprints (
    kind         TEXT NOT NULL,               -- text_sha | image_phash
    value        TEXT NOT NULL,
    phash        BIGINT,                      -- set for image_phash, used for Hamming-distance search
    claim_id     BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, value)
);

-- One investigation. Holds the trace summary used by the dashboard.
CREATE TABLE runs (
    id                UUID PRIMARY KEY,
    inbound_id        BIGINT REFERENCES inbound_messages(id) ON DELETE SET NULL,
    claim_id          BIGINT REFERENCES claims(id) ON DELETE SET NULL,
    purpose           TEXT NOT NULL DEFAULT 'check',  -- check | recheck | eval
    status            TEXT NOT NULL DEFAULT 'running', -- running | done | failed | budget
    verdict           TEXT,
    confidence        REAL,
    lang              TEXT,
    cache_hit         TEXT,                  -- none | text_sha | image_phash | claim_embedding
    steps             INTEGER NOT NULL DEFAULT 0,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cost_usd          NUMERIC(10,5) NOT NULL DEFAULT 0,
    latency_ms        INTEGER,
    verifier_passed   BOOLEAN,
    verifier_errors   JSONB NOT NULL DEFAULT '[]',
    trace             JSONB NOT NULL DEFAULT '{}',
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);
CREATE INDEX runs_started_idx ON runs (started_at);

-- Durable replay log: every LLM response and tool result of a run, in order.
CREATE TABLE run_journal (
    run_id      UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,                -- llm | tool
    key         TEXT NOT NULL,                -- hash of the request, guards against replaying a different step
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);

-- Sources fetched during a run. Quotes are verified against text in this table only.
CREATE TABLE evidence (
    run_id        UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    evidence_id   TEXT NOT NULL,              -- E1, E2, ... (stable within a run)
    url           TEXT NOT NULL,
    final_url     TEXT NOT NULL,
    title         TEXT,
    publisher     TEXT,
    tier          TEXT NOT NULL,              -- factchecker | official | news | other | satire
    label         TEXT,                       -- supports | refutes | context | scam_signal | satire_signal
    text          TEXT NOT NULL,
    text_sha256   TEXT NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, evidence_id)
);

-- Idempotent outbound messages. A reply is sent at most once per idempotency key.
CREATE TABLE outbox (
    id                   BIGSERIAL PRIMARY KEY,
    idempotency_key      TEXT NOT NULL UNIQUE,
    channel              TEXT NOT NULL,
    chat_ref_enc         BYTEA NOT NULL,
    body                 JSONB NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending', -- pending | sending | sent | failed
    attempts             INTEGER NOT NULL DEFAULT 0,
    provider_message_id  TEXT,
    last_error           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at              TIMESTAMPTZ
);

-- Durable job queue.
CREATE TABLE jobs (
    id               BIGSERIAL PRIMARY KEY,
    kind             TEXT NOT NULL,
    payload          JSONB NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | dead
    run_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 5,
    locked_by        TEXT,
    locked_until     TIMESTAMPTZ,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ
);
CREATE INDEX jobs_ready_idx ON jobs (run_at) WHERE status IN ('queued', 'running');

-- Low-confidence or high-stakes verdicts waiting for a human.
CREATE TABLE review_items (
    id                BIGSERIAL PRIMARY KEY,
    run_id            UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    claim_id          BIGINT REFERENCES claims(id) ON DELETE CASCADE,
    reason            TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open',   -- open | approved | corrected
    corrected_verdict TEXT,
    reviewer_note     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at        TIMESTAMPTZ,
    UNIQUE (run_id)
);

-- Human corrections exported into the eval set (scripts/export_corrections.py).
CREATE TABLE eval_corrections (
    id            BIGSERIAL PRIMARY KEY,
    claim_text    TEXT NOT NULL,
    lang          TEXT,
    verdict       TEXT NOT NULL,
    note          TEXT,
    source_run_id UUID,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    exported_at   TIMESTAMPTZ
);

-- Admin alerts, deduplicated by key (e.g. viral:<claim>:<day>).
CREATE TABLE admin_alerts (
    dedupe_key   TEXT PRIMARY KEY,
    claim_id     BIGINT REFERENCES claims(id) ON DELETE CASCADE,
    body         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
