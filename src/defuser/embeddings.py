"""Text embeddings for claim dedupe and clustering.

Default: a local feature-hashing embedder (character 3-5 grams + word uni/bi-grams, signed hashing into 256
dims, L2-normalised). It needs no vendor and is deterministic. It works across languages because what we embed
is the triage model's canonical English claim, not the raw forward. A hosted multilingual embedder can be
plugged in through the `Embedder` protocol if the claim texts drift too much in wording.
"""

import hashlib
import re
import unicodedata
from typing import Protocol

import numpy as np

DIM = 256
STOP = set(
    "a an the and or of to in on for is are was were be been will would from with by at as this that these those it its "
    "all any your you our we they he she them his her has have had do does did not no".split()
)


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


def normalize_claim(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _h(feature: str) -> tuple[int, float]:
    d = hashlib.blake2b(feature.encode(), digest_size=8).digest()
    n = int.from_bytes(d, "little")
    return n % DIM, 1.0 if (n >> 63) & 1 else -1.0


class HashingEmbedder:
    def embed(self, text: str) -> list[float]:
        t = normalize_claim(text)
        v = np.zeros(DIM, dtype=np.float64)
        words = [w for w in t.split() if w not in STOP]
        for w in words:
            i, s = _h("w:" + w)
            v[i] += 2.0 * s
        for a, b in zip(words, words[1:], strict=False):
            i, s = _h(f"b:{a}_{b}")
            v[i] += 1.5 * s
        joined = " ".join(words)
        for n in (3, 4, 5):
            for k in range(len(joined) - n + 1):
                i, s = _h(f"c{n}:" + joined[k : k + n])
                v[i] += 0.5 * s
        norm = np.linalg.norm(v)
        return (v / norm).tolist() if norm else v.tolist()


def cosine(a, b) -> float:
    a, b = np.asarray(a), np.asarray(b)
    d = np.linalg.norm(a) * np.linalg.norm(b)
    return float(a @ b / d) if d else 0.0


_embedder: Embedder = HashingEmbedder()


def get_embedder() -> Embedder:
    return _embedder


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_claim(text).encode()).hexdigest()
