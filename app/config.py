"""Runtime configuration via environment variables."""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    # General
    app_name: str = "Jaratrade API"
    environment: str = Field(default="development")
    debug: bool = Field(default=True)

    # Database
    database_url: str = Field(default="sqlite:///./jaratrade.db")

    # JWT
    jwt_secret: str = Field(default="dev-secret-change-me-in-production-please-change-this")
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 60 * 24 * 7  # 7 days

    # Fernet (used for legacy-compatible encrypted-id helpers - optional)
    fernet_key: str = Field(default="")

    # CORS
    cors_origins: List[str] = Field(default_factory=lambda: [
        "http://localhost:3000",
        "http://localhost:3030",
        "https://jaratrade.com",
    ])

    # Cloudinary
    cloudinary_cloud_name: str = Field(default="")
    cloudinary_api_key: str = Field(default="")
    cloudinary_api_secret: str = Field(default="")

    # Flutterwave
    flw_public_key: str = Field(default="")
    flw_secret_key: str = Field(default="")
    flw_encrypt_key: str = Field(default="")
    flw_commission_subaccount_id: str = Field(default="")
    # Shared secret echoed back in the `verif-hash` header on every webhook.
    # Set to the same string you put in Flutterwave's dashboard under
    # Settings -> Webhooks. Leave empty in dev to disable signature checks.
    flw_webhook_secret: str = Field(default="")

    # Email (transactional). Not required for dev.
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from: str = Field(default="no-reply@jaratrade.com")

    # Public site (used in email links etc.)
    site_url: str = Field(default="http://localhost:3030")

    # Platform fees
    free_commission_pct: float = Field(default=2.0)
    premium_commission_pct: float = Field(default=1.5)

    # Observability
    sentry_dsn: str = Field(default="")
    sentry_traces_sample_rate: float = Field(default=0.1)
    sentry_profiles_sample_rate: float = Field(default=0.0)
    otel_service_name: str = Field(default="jaratrade-api")
    otel_exporter_otlp_endpoint: str = Field(default="")
    # Comma-separated `key=value` pairs (e.g. `api-key=...`)
    otel_exporter_otlp_headers: str = Field(default="")
    # Set to true to force the dev console exporter even when an OTLP endpoint is configured
    otel_console_exporter: bool = Field(default=False)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
