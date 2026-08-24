"""Application settings. Values come from the environment with the ``APP_`` prefix."""

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The development signing key. Published in this repository, so it is a
#: sentinel for "not configured" rather than a usable secret.
DEV_SECRET_KEY = "unsafe-dummy-secret-key-for-local-dev"

MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://support:support@localhost:5432/support"

    # Consumed by the SLA monitor from P5 onward (docs/ARCHITECTURE.md §6).
    sla_scan_interval_seconds: int = 30

    # Signing key for JWTs. The default is convenient locally and is published in
    # this repository, so it is refused outright in production — see the validator
    # below. Generate a real one with `python -c "import secrets;
    # print(secrets.token_urlsafe(48))"` and set APP_SECRET_KEY.
    secret_key: str = DEV_SECRET_KEY
    access_token_expire_minutes: int = 60

    @model_validator(mode="after")
    def _reject_insecure_production_secret(self) -> "Settings":
        """Refuse to run production on a key an attacker can read.

        This repository is public: anyone can read the development default and
        mint a token for any role, including ADMIN. Failing at startup is the
        only safe behaviour — a warning would be ignored until it mattered.
        """
        if self.env != "production":
            return self

        if self.secret_key == DEV_SECRET_KEY:
            raise ValueError(
                "APP_SECRET_KEY is still the published development default. "
                "Set a unique secret before running in production."
            )
        if len(self.secret_key) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(f"APP_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters.")
        return self

    # Attachment limits (docs/API.md §8 requires max size + allowed content types).
    attachment_max_bytes: int = 10 * 1024 * 1024
    attachment_allowed_content_types: tuple[str, ...] = (
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/csv",
        "application/zip",
        "application/json",
    )


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so settings are parsed once per process."""
    return Settings()
