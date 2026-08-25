"""Deployment configuration guards (rules.md §1, §6).

These exist because of a real deployment failure, not a hypothetical one. The
backend was configured with `APP_DATABASE_URL` on Railway, which pydantic
ignores — `database_url` carries `alias="DATABASE_URL"`, and an alias bypasses
`env_prefix` entirely. The value fell back to the localhost default and the only
symptom was `Connect call failed ('127.0.0.1', 5432)` deep in an asyncpg
traceback, naming neither the variable nor the mistake.
"""

import logging

import pytest

from app.config import DEV_SECRET_KEY, LOCAL_DATABASE_URL, Settings

REAL_SECRET = "x" * 40
RAILWAY_URL = "postgresql://postgres:pw@postgres.railway.internal:5432/railway"


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in (
        "DATABASE_URL",
        "APP_DATABASE_URL",
        "FRONTEND_ORIGIN",
        "APP_ENV",
        "APP_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    # `_env_file=None` so a developer's local .env cannot make this pass.
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestUnprefixedNames:
    """rules.md §1 names `DATABASE_URL` and `FRONTEND_ORIGIN` with no prefix."""

    def test_database_url_is_read_without_the_app_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(monkeypatch, DATABASE_URL=RAILWAY_URL)
        assert "postgres.railway.internal" in settings.database_url

    def test_the_prefixed_name_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**The bug that broke the deploy.**

        `APP_DATABASE_URL` is not a second accepted spelling — it does nothing,
        and the localhost default silently wins. Pinned so nobody re-derives
        the prefixed name from `env_prefix` and loses an evening to it.
        """
        settings = _settings(monkeypatch, APP_DATABASE_URL=RAILWAY_URL)
        assert settings.database_url == LOCAL_DATABASE_URL

    def test_frontend_origin_is_read_without_the_app_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(monkeypatch, FRONTEND_ORIGIN="https://app.vercel.app")
        assert settings.frontend_origin == "https://app.vercel.app"


class TestRailwayCompatibility:
    def test_a_pasted_railway_url_needs_no_hand_editing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`${{Postgres.DATABASE_URL}}` yields a `postgresql://` URL. asyncpg
        needs `postgresql+asyncpg://`, so the conversion happens here rather
        than in a deployment runbook step someone will skip."""
        settings = _settings(monkeypatch, DATABASE_URL=RAILWAY_URL)
        assert settings.database_url.startswith("postgresql+asyncpg://")
        assert settings.database_url.endswith("@postgres.railway.internal:5432/railway")

    def test_an_already_converted_url_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        explicit = "postgresql+asyncpg://u:p@host:5432/db"
        assert _settings(monkeypatch, DATABASE_URL=explicit).database_url == explicit


class TestProductionGuards:
    def test_localhost_in_production_is_reported_by_name(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A log line, not a raise: rules.md §3 wants the server to start so the
        failure is reachable at `/health/db` instead of an opaque 502."""
        with caplog.at_level(logging.ERROR):
            settings = _settings(monkeypatch, APP_ENV="production", APP_SECRET_KEY=REAL_SECRET)

        assert settings.database_url == LOCAL_DATABASE_URL, "it still starts"
        assert "DATABASE_URL" in caplog.text
        assert "APP_DATABASE_URL" in caplog.text, "the message names the wrong spelling too"

    def test_a_configured_production_says_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR):
            _settings(
                monkeypatch,
                APP_ENV="production",
                APP_SECRET_KEY=REAL_SECRET,
                DATABASE_URL=RAILWAY_URL,
            )
        assert caplog.text == ""

    def test_development_is_not_nagged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # localhost is the correct answer locally.
        with caplog.at_level(logging.ERROR):
            _settings(monkeypatch)
        assert caplog.text == ""

    def test_the_published_dev_secret_still_refuses_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # This one DOES raise — a readable signing key is not a "start anyway".
        with pytest.raises(ValueError, match="development default"):
            _settings(monkeypatch, APP_ENV="production", APP_SECRET_KEY=DEV_SECRET_KEY)
