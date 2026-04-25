"""Unit tests for Telegram initData HMAC verification."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode


def _fake_init_data(bot_token: str, *, user_id: int = 42, expired: bool = False) -> str:
    auth_date = int(time.time()) - (25 * 60 * 60 if expired else 0)
    payload = {
        "user": f'{{"id": {user_id}, "first_name": "A", "username": "u"}}',
        "auth_date": str(auth_date),
        "query_id": "test",
    }
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    data_check_string = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    payload["hash"] = expected
    return urlencode(payload)


def test_init_data_round_trip() -> None:
    os.environ["BOT_TOKEN"] = "12345:fake-test-token"
    # Reimport to pick up the env change cleanly — module caches _BOT_TOKEN at import.
    import importlib
    from api import auth

    importlib.reload(auth)
    raw = _fake_init_data(os.environ["BOT_TOKEN"])
    parsed = auth._verify_init_data(raw)
    assert parsed["user"].startswith('{"id": 42')


def test_init_data_expired_rejected() -> None:
    os.environ["BOT_TOKEN"] = "12345:fake-test-token"
    import importlib

    from fastapi import HTTPException

    from api import auth

    importlib.reload(auth)
    raw = _fake_init_data(os.environ["BOT_TOKEN"], expired=True)
    try:
        auth._verify_init_data(raw)
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("expected HTTPException")


def test_init_data_bad_hash_rejected() -> None:
    os.environ["BOT_TOKEN"] = "12345:fake-test-token"
    import importlib

    from fastapi import HTTPException

    from api import auth

    importlib.reload(auth)
    raw = _fake_init_data(os.environ["BOT_TOKEN"]) + "tamper"
    try:
        auth._verify_init_data(raw)
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("expected HTTPException")
