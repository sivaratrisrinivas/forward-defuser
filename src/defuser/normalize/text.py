"""Text cleanup, URL extraction and a fast script-based language guess (the triage model refines it)."""

import re
import unicodedata

URL_RE = re.compile(r"(?i)\b((?:https?://|www\.)[^\s<>\"'()]+[^\s<>\"'().,;:!?])")
# Bare domains like "pm-yojana.in/free" that forwards often contain without a scheme.
BARE_DOMAIN_RE = re.compile(
    r"(?i)\b([a-z0-9][a-z0-9-]{1,62}\.(?:in|com|org|net|info|xyz|top|online|site|live|co|me|ly|cc)(?:/[^\s]*)?)"
)

SCRIPTS = {
    "hi": (0x0900, 0x097F),  # Devanagari (Hindi, Marathi share it; Marathi needs the model)
    "bn": (0x0980, 0x09FF),
    "ta": (0x0B80, 0x0BFF),
    "te": (0x0C00, 0x0C7F),
}

# Small romanized-Hindi marker list for Hinglish detection. Function words, not content words.
HINGLISH_MARKERS = {
    "hai",
    "hain",
    "nahi",
    "nahin",
    "kya",
    "kyu",
    "kyun",
    "aur",
    "ka",
    "ki",
    "ke",
    "ko",
    "se",
    "mein",
    "main",
    "bhi",
    "toh",
    "to",
    "kar",
    "karo",
    "karein",
    "kare",
    "raha",
    "rahi",
    "rahe",
    "wala",
    "wale",
    "sab",
    "sabhi",
    "abhi",
    "ye",
    "yeh",
    "woh",
    "wo",
    "jaldi",
    "bhai",
    "bhaiyo",
    "dosto",
    "zaroor",
    "jarur",
    "pakka",
    "sach",
    "jhooth",
    "hoga",
    "hogi",
    "gaya",
    "gayi",
    "diya",
    "liye",
    "lie",
    "batao",
    "share",
    "forward",
}


def clean(text: str, max_chars: int) -> tuple[str, bool]:
    text = unicodedata.normalize("NFC", text or "")
    text = re.sub(r"[​-‏‪-‮⁠-⁤﻿]", "", text)  # zero-width / bidi controls
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def extract_urls(text: str, limit: int = 5) -> list[str]:
    seen: list[str] = []
    for m in URL_RE.finditer(text):
        u = m.group(1)
        if not u.lower().startswith("http"):
            u = "https://" + u
        if u not in seen:
            seen.append(u)
    if not seen:
        for m in BARE_DOMAIN_RE.finditer(text):
            u = "https://" + m.group(1)
            if u not in seen:
                seen.append(u)
    return seen[:limit]


def guess_lang(text: str, hint: str | None = None) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return (hint or "en")[:2] if (hint or "en")[:2] in {"en", "hi"} else "en"
    counts = {k: 0 for k in SCRIPTS}
    for c in letters:
        cp = ord(c)
        for lang, (lo, hi) in SCRIPTS.items():
            if lo <= cp <= hi:
                counts[lang] += 1
    lang, n = max(counts.items(), key=lambda kv: kv[1])
    if n / len(letters) > 0.3:
        return lang
    words = re.findall(r"[a-z]+", text.lower())
    if words:
        markers = sum(1 for w in words if w in HINGLISH_MARKERS)
        if markers >= 2 and markers / len(words) >= 0.12:
            return "hinglish"
    return "en"
