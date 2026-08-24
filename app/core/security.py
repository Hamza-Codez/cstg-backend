from datetime import timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.config import get_settings
from app.core import clock

ph = PasswordHasher()
ALGORITHM = "HS256"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bool(ph.verify(hashed_password, plain_password))
    except VerifyMismatchError:
        return False


def get_password_hash(password: str) -> str:
    return str(ph.hash(password))


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    settings = get_settings()
    to_encode = data.copy()

    if expires_delta:
        expire = clock.now() + expires_delta
    else:
        expire = clock.now() + timedelta(minutes=settings.access_token_expire_minutes)

    to_encode.update({"exp": expire, "iat": clock.now()})

    encoded_jwt = jwt.encode(to_encode, settings.secret_key, algorithm=ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    # jwt.decode validates expiration automatically based on "exp"
    return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
