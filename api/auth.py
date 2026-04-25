"""Telegram WebApp initData HMAC verification.

Spec:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import HTTPException, Header, status

_BOT_TOKEN = os.getenv("BOT_TOKEN", "")
_AUTH_MODE = os.getenv("API_AUTH_MODE", "telegram")  # telegram | local | both
_MAX_AGE_SECONDS = 24 * 60 * 60  # 24h tolerance per Telegram docs


@dataclass
class TelegramUser:
    id: int
    username: str | None
    first_name: str | None
    language_code: str | None


def _verify_init_data(init_data: str) -> dict[str, str]:
    """Return the parsed, verified key/value map from ``initData``.

    Raises :class:`HTTPException` on any failure.
    """
    if not _BOT_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="BOT_TOKEN missing on server",
        )

    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="no hash in initData")

    auth_date = pairs.get("auth_date")
    if auth_date and time.time() - int(auth_date) > _MAX_AGE_SECONDS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="initData expired")

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", _BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="bad initData hash")
    return pairs


def telegram_user_from_header(
    x_telegram_init_data: Optional[str] = Header(default=None, alias="X-Telegram-Init-Data"),
) -> TelegramUser | None:
    """FastAPI dependency: verify + decode the `user` field from initData.

    - ``telegram`` mode: always requires a valid header.
    - ``local`` mode: always allows anonymous access.
    - ``both`` mode: uses the header when present, otherwise allows anonymous.
    """
    if _AUTH_MODE == "local":
        return None
    if not x_telegram_init_data:
        if _AUTH_MODE == "both":
            return None
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing initData header")

    parsed = _verify_init_data(x_telegram_init_data)
    user_raw = parsed.get("user")
    if not user_raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="no user in initData")
    try:
        user_obj = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="malformed user") from exc
    return TelegramUser(
        id=int(user_obj["id"]),
        username=user_obj.get("username"),
        first_name=user_obj.get("first_name"),
        language_code=user_obj.get("language_code"),
    )


def require_telegram_user(user: TelegramUser | None) -> TelegramUser:
    """Enforce authenticated user when handlers need one even in ``local`` mode."""
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Telegram initData required"
        )
    return user
