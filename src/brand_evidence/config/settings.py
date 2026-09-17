"""Runtime configuration. Validates at startup and raises on anything incomplete."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Required: these affect the record, so no silent defaults.
    db_path: Path
    store_path: Path
    digest_path: Path
    brand_terms: Annotated[list[str], NoDecode]
    archive_provider: Literal["wayback"]
    # Save Page Now requires an archive.org account; keys come from https://archive.org/account/s3.php
    wayback_access_key: str = ""
    wayback_secret_key: str = ""

    # Optional sources; a missing credential disables the source with a warning.
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    google_alerts_feeds: Annotated[list[str], NoDecode] = Field(default_factory=list)
    web_search_provider: Literal["", "brave"] = ""
    web_search_api_key: str = ""

    # Goes in the User-Agent of every request, so the sites this visits can see
    # who is running it. Empty means the User-Agent carries no URL.
    contact_url: str = ""

    # RFC 3161 Time Stamping Authority. Empty disables timestamps (exports say so).
    tsa_url: str = "https://freetsa.org/tsr"

    # Optional: JSON feed of the owner's own publications (push path fallback when the
    # publisher runs on a server). Items: platform, external_url, external_id, published_at, text.
    own_publications_url: str = ""
    own_publications_token: str = ""
    own_publications_auth: Literal["api_key", "bearer"] = "api_key"

    enable_classification: bool = False
    anthropic_api_key: str = ""

    smtp_url: str = ""
    digest_email_to: str = ""
    digest_email_from: str = ""

    log_level: Literal["debug", "info", "warning", "error"] = "info"

    # Capture limits (§6). Constants rather than env so the record stays comparable.
    viewport_width: int = 1440
    viewport_height: int = 900
    device_scale_factor: int = 2
    max_captures_per_run: int = 20
    archive_min_interval_seconds: float = 10.0

    @field_validator("brand_terms", "google_alerts_feeds", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("brand_terms")
    @classmethod
    def _terms_not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("BE_BRAND_TERMS must list at least one term")
        return value

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        if self.archive_provider == "wayback" and not (
            self.wayback_access_key and self.wayback_secret_key
        ):
            raise ValueError(
                "BE_ARCHIVE_PROVIDER=wayback requires BE_WAYBACK_ACCESS_KEY and "
                "BE_WAYBACK_SECRET_KEY (free: https://archive.org/account/s3.php)"
            )
        if self.enable_classification and not self.anthropic_api_key:
            raise ValueError("BE_ENABLE_CLASSIFICATION=true requires BE_ANTHROPIC_API_KEY")
        if self.web_search_api_key and not self.web_search_provider:
            raise ValueError("BE_WEB_SEARCH_API_KEY set but BE_WEB_SEARCH_PROVIDER is empty")
        if bool(self.reddit_client_id) != bool(self.reddit_client_secret):
            raise ValueError("Reddit needs both BE_REDDIT_CLIENT_ID and BE_REDDIT_CLIENT_SECRET")
        if self.smtp_url and not (self.digest_email_to and self.digest_email_from):
            raise ValueError("BE_SMTP_URL requires BE_DIGEST_EMAIL_TO and BE_DIGEST_EMAIL_FROM")
        return self

    @property
    def tsa_enabled(self) -> bool:
        return bool(self.tsa_url)

    @property
    def own_publications_enabled(self) -> bool:
        return bool(self.own_publications_url)

    @property
    def reddit_enabled(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def web_search_enabled(self) -> bool:
        return bool(self.web_search_provider and self.web_search_api_key)

    @property
    def sqlalchemy_url(self) -> str:
        return f"sqlite:///{self.db_path}"


def load_settings(env_file: Path | str | None = None) -> Settings:
    """Load and validate settings. Raises pydantic.ValidationError when incomplete."""
    if env_file is not None:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    return Settings()  # type: ignore[call-arg]
