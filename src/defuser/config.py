"""Settings. Every secret comes from the environment (or Secret Manager mounted as env); nothing is hard-coded."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    public_base_url: str = "http://localhost:8080"
    log_level: str = "INFO"

    database_url: str = "postgresql://fd:fd@localhost:5432/fd"
    redis_url: str = "redis://localhost:6379/0"

    # Privacy
    hash_pepper: SecretStr = SecretStr("dev-pepper-change-me")
    data_key: SecretStr = SecretStr("")  # Fernet key; generate with scripts/gen_keys.py
    retention_days: int = 30

    # LLM
    anthropic_api_key: SecretStr | None = None
    model_main: str = "claude-opus-5-5"
    model_cheap: str = "claude-haiku-5-5"
    llm_provider: str = "anthropic"  # anthropic | fake
    # USD per million tokens, "input:output" per model. Used for budgets and the dashboard.
    model_prices: str = "claude-opus-5-5=4:20,claude-haiku-5-5=0.1:0.5,claude-sonnet-5-5=2:10"

    # Channels
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_bot_username: str = ""
    whatsapp_access_token: SecretStr | None = None
    whatsapp_app_secret: SecretStr | None = None
    whatsapp_verify_token: SecretStr | None = None
    whatsapp_phone_number_id: str = ""
    whatsapp_graph_version: str = "v21.0"
    admin_telegram_chat_id: str = ""

    # Tools
    google_api_key: SecretStr | None = None  # Fact Check Tools, Vision, Safe Browsing, Speech-to-Text
    brave_api_key: SecretStr | None = None
    factcheck_feeds: str = ""  # comma separated RSS/Atom URLs of fact-checkers to index
    fetch_timeout_s: float = 8.0
    fetch_max_bytes: int = 3_000_000
    fetch_max_redirects: int = 5

    # Object storage (S3 compatible). Used for media while a job is in flight.
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None

    # Budgets
    max_steps_per_message: int = 8
    max_cost_per_message_usd: float = 0.25
    max_cost_per_user_day_usd: float = 1.0
    max_messages_per_user_day: int = 40
    max_messages_per_user_minute: int = 5
    max_cost_global_day_usd: float = 50.0

    # Cache and clustering
    claim_cache_similarity: float = 0.90
    claim_cluster_similarity: float = 0.82
    cache_fresh_hours_verified: int = 24 * 30
    cache_fresh_hours_unverified: int = 6
    image_phash_max_distance: int = 6
    viral_threshold_24h: int = 25

    # Limits on inputs
    max_text_chars: int = 6000
    max_image_bytes: int = 10_000_000
    max_voice_seconds: int = 300

    # Follow-ups
    followup_hours: str = "6,24,72"

    # Admin
    admin_token: SecretStr | None = None

    # Observability
    otel_exporter_otlp_endpoint: str = ""
    worker_concurrency: int = Field(default=4, ge=1)

    def prices(self) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for item in filter(None, (p.strip() for p in self.model_prices.split(","))):
            name, rates = item.split("=")
            i, o = rates.split(":")
            out[name.strip()] = (float(i), float(o))
        return out

    def followup_schedule(self) -> list[int]:
        return [int(h) for h in self.followup_hours.split(",") if h.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
