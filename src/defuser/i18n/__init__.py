"""Per-language reply templates. Add a language by dropping `<code>.yaml` here and listing it in
SUPPORTED_LANGS; the composer writes reasons in any language the model handles."""

import hashlib
import os
from functools import lru_cache
from pathlib import Path

import yaml

_DIR = Path(__file__).parent
FALLBACK = "en"


@lru_cache
def _load(code: str) -> dict:
    path = _DIR / f"{code}.yaml"
    if not path.exists():
        return {}
    return _fix_keys(yaml.safe_load(path.read_text(encoding="utf-8")))


def _fix_keys(obj):
    # YAML 1.1 reads bare TRUE/FALSE keys as booleans; verdict names must stay strings.
    if isinstance(obj, dict):
        return {(str(k).upper() if isinstance(k, bool) else k): _fix_keys(v) for k, v in obj.items()}
    return obj


def supported() -> list[str]:
    return [
        c.strip()
        for c in os.environ.get("SUPPORTED_LANGS", "en,hi,hinglish,ta,te,bn,mr").split(",")
        if c.strip()
    ]


def resolve(code: str | None) -> str:
    code = (code or FALLBACK).lower()
    return code if code in supported() and _load(code) else FALLBACK


def t(code: str, key: str, sub: str | None = None) -> str:
    for c in (resolve(code), FALLBACK):
        val = _load(c).get(key)
        if isinstance(val, dict) and sub is not None:
            val = val.get(sub)
        if isinstance(val, list):
            val = val[0] if val else None
        if val:
            return val
    raise KeyError(f"{key}.{sub}")


def pick(code: str, key: str, sub: str, seed: str) -> str:
    """Deterministic choice among variants (so a re-run renders the same text)."""
    for c in (resolve(code), FALLBACK):
        val = (_load(c).get(key) or {}).get(sub)
        if val:
            options = val if isinstance(val, list) else [val]
            idx = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(options)
            return options[idx]
    return ""


def language_name(code: str) -> str:
    return _load(resolve(code)).get("name", "English")
