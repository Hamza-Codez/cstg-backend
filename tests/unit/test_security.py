from datetime import timedelta

from app.core import security


def test_password_hashing() -> None:
    password = "supersecret"
    hashed = security.get_password_hash(password)
    assert hashed != password
    assert security.verify_password(password, hashed) is True
    assert security.verify_password("wrong", hashed) is False


def test_jwt_encoding_decoding() -> None:
    data = {"sub": "123", "role": "ADMIN"}
    token = security.create_access_token(data=data, expires_delta=timedelta(minutes=5))
    decoded = security.decode_access_token(token)
    assert decoded["sub"] == "123"
    assert decoded["role"] == "ADMIN"
    assert "exp" in decoded
    assert "iat" in decoded
