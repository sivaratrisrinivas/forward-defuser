"""Near-duplicate cache.

Most forwards are repeats, so before paying for an investigation we look for:
1. an exact repeat (sha256 of the normalised text) or the same image (64-bit pHash within Hamming distance N),
2. after triage, the same claim (cosine similarity of canonical-English claim embeddings >= threshold), which
   also catches the same rumour arriving in another language.
A hit is only used if it is fresh: verified verdicts for CACHE_FRESH_HOURS_VERIFIED, UNVERIFIED for
CACHE_FRESH_HOURS_UNVERIFIED (so new coverage gets picked up). Stale hits fall through to a full check.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

from defuser.config import get_settings
from defuser.db import vec
from defuser.embeddings import get_embedder, text_fingerprint
from defuser.models import CanonicalMessage, Draft, Evidence, Label, Tier, Verdict


@dataclass
class CachedVerdict:
    claim_id: int
    verdict: Verdict
    confidence: float
    draft: Draft | None
    evidence: dict[str, Evidence]
    lang: str | None
    hit: str  # text_sha | image_phash | claim_embedding
    similarity: float = 1.0
    run_id: str | None = None


def phash_int(phash_hex: str) -> int:
    v = int(phash_hex, 16)
    return v - (1 << 64) if v >= 1 << 63 else v  # store as signed BIGINT


class ClaimCache:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    def _fresh(self, row) -> bool:
        s = get_settings()
        if row["verdict_at"] is None:
            return False
        hours = (
            s.cache_fresh_hours_unverified if row["verdict"] == "UNVERIFIED" else s.cache_fresh_hours_verified
        )
        return datetime.now(UTC) - row["verdict_at"] < timedelta(hours=hours)

    async def _load(self, claim_id: int, hit: str, similarity: float = 1.0) -> CachedVerdict | None:
        row = await self.pool.fetchrow(
            "SELECT c.id, c.verdict, c.confidence, c.verdict_at, c.verdict_run_id, r.trace, r.lang "
            "FROM claims c LEFT JOIN runs r ON r.id = c.verdict_run_id WHERE c.id = $1",
            claim_id,
        )
        if row is None or not self._fresh(row):
            return None
        draft = None
        evidence: dict[str, Evidence] = {}
        if row["trace"] and row["trace"].get("draft"):
            draft = Draft(**row["trace"]["draft"])
            for e in await self.pool.fetch(
                "SELECT evidence_id, url, final_url, title, publisher, tier, label, text FROM evidence WHERE run_id = $1",
                row["verdict_run_id"],
            ):
                evidence[e["evidence_id"]] = Evidence(
                    evidence_id=e["evidence_id"],
                    url=e["url"],
                    final_url=e["final_url"],
                    title=e["title"],
                    publisher=e["publisher"],
                    tier=Tier(e["tier"]),
                    label=Label(e["label"]) if e["label"] else None,
                    text=e["text"],
                )
        return CachedVerdict(
            claim_id=row["id"],
            verdict=Verdict(row["verdict"]),
            confidence=row["confidence"],
            draft=draft,
            evidence=evidence,
            lang=row["lang"],
            hit=hit,
            similarity=similarity,
            run_id=str(row["verdict_run_id"]) if row["verdict_run_id"] else None,
        )

    async def by_fingerprint(self, msg: CanonicalMessage) -> CachedVerdict | None:
        text = msg.combined_text()
        if len(text) >= 20:
            cid = await self.pool.fetchval(
                "SELECT claim_id FROM fingerprints WHERE kind = 'text_sha' AND value = $1",
                text_fingerprint(text),
            )
            if cid:
                return await self._load(cid, "text_sha")
        if msg.image and msg.image.phash:
            cid = await self.pool.fetchval(
                """SELECT claim_id FROM fingerprints WHERE kind = 'image_phash'
                   AND bit_count((phash # $1)::bit(64)) <= $2
                   ORDER BY bit_count((phash # $1)::bit(64)) LIMIT 1""",
                phash_int(msg.image.phash),
                get_settings().image_phash_max_distance,
            )
            if cid:
                return await self._load(cid, "image_phash")
        return None

    async def nearest(self, canonical: str) -> tuple[int, float, str] | None:
        emb = get_embedder().embed(canonical)
        row = await self.pool.fetchrow(
            "SELECT id, canonical_text, 1 - (embedding <=> $1::vector) AS sim FROM claims "
            "ORDER BY embedding <=> $1::vector LIMIT 1",
            vec(emb),
        )
        return (row["id"], float(row["sim"]), row["canonical_text"]) if row else None

    async def candidate(self, canonical: str) -> tuple[int, float, str] | None:
        """Nearest stored claim above the candidate threshold. The caller confirms it is the same claim."""
        near = await self.nearest(canonical)
        if near and near[1] >= get_settings().claim_cache_similarity:
            return near
        return None

    async def load(self, claim_id: int, similarity: float) -> CachedVerdict | None:
        return await self._load(claim_id, "claim_embedding", similarity)

    async def record(
        self,
        canonical: str,
        claim_type: str,
        msg: CanonicalMessage,
        verdict: Verdict,
        confidence: float,
        run_id: str,
        update_verdict: bool = True,
        claim_id: int | None = None,
    ) -> int:
        """Attach this check to a confirmed-same claim (`claim_id`) or create a new claim, and store the message
        fingerprints."""
        s = get_settings()
        emb = get_embedder().embed(canonical)
        async with self.pool.acquire() as conn, conn.transaction():
            if claim_id is not None:
                await conn.execute(
                    "UPDATE claims SET ask_count = ask_count + 1, last_seen_at = now() WHERE id = $1",
                    claim_id,
                )
            else:
                claim_id = await conn.fetchval(
                    "INSERT INTO claims (canonical_text, claim_type, embedding, ask_count) VALUES ($1, $2, $3::vector, 1) "
                    "RETURNING id",
                    canonical[:1000],
                    claim_type,
                    vec(emb),
                )
            if update_verdict:
                # A fresh verified verdict always wins; an UNVERIFIED result never overwrites a verified one that is
                # still fresh.
                await conn.execute(
                    """UPDATE claims SET verdict = $2, confidence = $3, verdict_run_id = $4, verdict_at = now(),
                              checked_at = now()
                       WHERE id = $1 AND ($2 <> 'UNVERIFIED' OR verdict = 'UNVERIFIED'
                             OR verdict_at < now() - make_interval(hours => $5))""",
                    claim_id,
                    verdict.value,
                    confidence,
                    run_id,
                    s.cache_fresh_hours_verified,
                )
            text = msg.combined_text()
            if len(text) >= 20:
                await conn.execute(
                    "INSERT INTO fingerprints (kind, value, claim_id) VALUES ('text_sha', $1, $2) ON CONFLICT DO NOTHING",
                    text_fingerprint(text),
                    claim_id,
                )
            if msg.image and msg.image.phash:
                await conn.execute(
                    "INSERT INTO fingerprints (kind, value, phash, claim_id) VALUES ('image_phash', $1, $2, $3) "
                    "ON CONFLICT DO NOTHING",
                    msg.image.phash,
                    phash_int(msg.image.phash),
                    claim_id,
                )
        return claim_id
