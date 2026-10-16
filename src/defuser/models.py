"""Domain objects shared by every stage of the pipeline."""

from enum import StrEnum

from pydantic import BaseModel, Field


class Verdict(StrEnum):
    FALSE = "FALSE"
    MISLEADING = "MISLEADING"
    TRUE = "TRUE"
    SATIRE = "SATIRE"
    SCAM = "SCAM"
    UNVERIFIED = "UNVERIFIED"


class Tier(StrEnum):
    FACTCHECKER = "factchecker"
    OFFICIAL = "official"
    NEWS = "news"
    OTHER = "other"
    SATIRE = "satire"


class Label(StrEnum):
    SUPPORTS = "supports"
    REFUTES = "refutes"
    CONTEXT = "context"
    SCAM_SIGNAL = "scam_signal"
    SATIRE_SIGNAL = "satire_signal"


class ChatRef(BaseModel):
    """Everything needed to reply. Stored only encrypted."""

    channel: str
    chat_id: str
    reply_to_message_id: str | None = None
    is_group: bool = False


class LinkContent(BaseModel):
    url: str
    final_url: str
    redirect_chain: list[str] = []
    title: str | None = None
    text: str = ""
    error: str | None = None


class ImageContent(BaseModel):
    storage_key: str | None = None
    phash: str | None = None
    ocr_text: str = ""
    description: str = ""
    web_entities: list[str] = []
    matching_pages: list[dict] = []  # [{url, title}] from reverse image search
    error: str | None = None


class AudioContent(BaseModel):
    duration_s: float = 0
    transcript: str = ""
    truncated: bool = False
    error: str | None = None


class CanonicalMessage(BaseModel):
    channel: str
    message_id: str
    user_hash: str
    chat: ChatRef
    text: str = ""
    links: list[LinkContent] = []
    image: ImageContent | None = None
    audio: AudioContent | None = None
    lang: str = "en"
    forwarded: bool = False
    forwarded_many_times: bool = False
    truncated: bool = False

    def combined_text(self) -> str:
        parts = [self.text]
        if self.image:
            parts += [self.image.ocr_text, self.image.description]
        if self.audio:
            parts.append(self.audio.transcript)
        return "\n".join(p for p in parts if p).strip()


class ClaimType(StrEnum):
    HEALTH = "health"
    POLITICS = "politics"
    FINANCE_SCAM = "finance_scam"
    OLD_PHOTO = "old_photo_new_context"
    DEEPFAKE = "deepfake"
    SATIRE = "satire"
    OPINION = "opinion"
    OTHER = "other"


class Claim(BaseModel):
    text: str
    canonical_en: str


class TriageResult(BaseModel):
    claims: list[Claim] = []
    claim_type: ClaimType = ClaimType.OTHER
    checkable: bool = True
    lang: str = "en"
    injection_suspected: bool = False


class Evidence(BaseModel):
    evidence_id: str
    url: str
    final_url: str
    title: str | None = None
    publisher: str | None = None
    tier: Tier = Tier.OTHER
    kind: str = "page"  # page | factcheck_record | safety_record | image_match
    label: Label | None = None
    text: str


class Citation(BaseModel):
    evidence_id: str
    quote: str


class Reason(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)


class Draft(BaseModel):
    verdict: Verdict = Verdict.UNVERIFIED
    confidence: float = 0.0
    reasons: list[Reason] = Field(default_factory=list)


class VerifierError(BaseModel):
    code: str
    detail: str


class VerifierReport(BaseModel):
    passed: bool
    errors: list[VerifierError] = []
