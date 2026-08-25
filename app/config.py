"""Application settings. Values come from the environment with the ``APP_`` prefix."""

import logging
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The development signing key. Published in this repository, so it is a
#: sentinel for "not configured" rather than a usable secret.
DEV_SECRET_KEY = "unsafe-dummy-secret-key-for-local-dev"

MIN_SECRET_KEY_LENGTH = 32

#: The local-development database. Like DEV_SECRET_KEY, it doubles as a sentinel
#: for "not configured": a production process pointed here is misconfigured, not
#: unlucky.
LOCAL_DATABASE_URL = "postgresql+asyncpg://support:support@localhost:5432/support"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    #: `alias` deliberately BYPASSES `env_prefix`, so this reads `DATABASE_URL`
    #: and NOT `APP_DATABASE_URL` — matching rules.md §1 and letting Railway's
    #: `${{Postgres.DATABASE_URL}}` reference be pasted in unchanged.
    #:
    #: The trap this creates is worth knowing: setting `APP_DATABASE_URL` is
    #: silently ignored, and the default below then points production at
    #: localhost. `_reject_local_database_in_production` exists to catch that.
    database_url: str = Field(
        LOCAL_DATABASE_URL,
        alias="DATABASE_URL",
    )
    #: Likewise unprefixed: `FRONTEND_ORIGIN`, not `APP_FRONTEND_ORIGIN`.
    frontend_origin: str = Field("", alias="FRONTEND_ORIGIN")

    @model_validator(mode="after")
    def _normalize_database_url(self) -> "Settings":
        if self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )
        return self

    # Consumed by the SLA monitor from P5 onward (docs/ARCHITECTURE.md §6).
    sla_scan_interval_seconds: int = 30

    # How long after resolution a ticket may still be reopened (T6, spec05 §4).
    # Unbounded reopen would let a year-old ticket return and immediately
    # breach against a year-old deadline.
    reopen_window_days: int = 14

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

    @model_validator(mode="after")
    def _warn_on_local_database_in_production(self) -> "Settings":
        """Say so, loudly, when production is still pointed at localhost.

        Deliberately a log line and not a raise: rules.md §3 wants the server to
        start even when the database is unreachable, so the failure surfaces at
        `/health/db` rather than as an opaque 502 with no logs.

        But silence was worse. A `DATABASE_URL` that never arrived — because it
        was set as `APP_DATABASE_URL`, which the alias above makes pydantic
        ignore — falls back to this default and produces
        `Connect call failed ('127.0.0.1', 5432)` deep in an asyncpg traceback,
        which names neither the variable nor the mistake. This turns hours of
        that into one line.
        """
        if self.env == "production" and self.database_url == LOCAL_DATABASE_URL:
            logging.getLogger(__name__).error(
                "DATABASE_URL is unset in production, so this process is pointed at "
                "localhost and every query will fail. Note the name: DATABASE_URL, "
                "NOT APP_DATABASE_URL — this field is aliased, so the APP_ prefix "
                "does not apply and a prefixed value is ignored silently. "
                "On Railway, ${{Postgres.DATABASE_URL}} can be pasted in as-is; the "
                "postgresql:// scheme is converted to asyncpg automatically.",
            )
        return self

    # Attachment limits (docs/API.md §8 requires max size + allowed content types).
    # Storage backend for attachment bytes (spec03 §3). ``local`` writes under
    # ``storage_root``; ``memory`` is for tests.
    #
    # DEPLOYMENT: ``local`` loses every object on redeploy where the filesystem
    # is ephemeral (IMPLEMENTATION_V2.md R14). Production needs a persistent
    # volume mounted at ``storage_root``, or an object-storage backend.
    storage_backend: Literal["local", "memory"] = "local"
    storage_root: str = "."

    # A truncated export that does not announce the truncation is a wrong
    # answer, so the cap is a refusal rather than a silent cut (spec09 §6).
    export_max_rows: int = 50_000

    attachment_max_bytes: int = 10 * 1024 * 1024
    # Without a count cap, "customers may upload" is an unbounded disk write for
    # any authenticated account (spec03 §6).
    attachment_max_per_ticket: int = 20
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

    bulk_max_items: int = 100


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so settings are parsed once per process."""
    return Settings()
