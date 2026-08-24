"""Production refuses the published development signing key."""

import pytest
from pydantic import ValidationError

from app.config import DEV_SECRET_KEY, Settings

REAL_KEY = "k" * 48


def test_development_may_use_the_default_key() -> None:
    assert Settings(env="development").secret_key == DEV_SECRET_KEY


def test_tests_may_use_the_default_key() -> None:
    assert Settings(env="test").secret_key == DEV_SECRET_KEY


def test_production_refuses_the_default_key() -> None:
    """The default is public in this repository — anyone could forge an admin token."""
    with pytest.raises(ValidationError, match="development default"):
        Settings(env="production")


def test_production_refuses_a_short_key() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(env="production", secret_key="tooshort")


def test_production_accepts_a_real_key() -> None:
    assert Settings(env="production", secret_key=REAL_KEY).secret_key == REAL_KEY
