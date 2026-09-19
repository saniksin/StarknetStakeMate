"""Запасной путь ?tg_id= должен работать только с самой машины.

Раньше единственной преградой между интернетом и данными любого пользователя
было одно слово в .env: при API_AUTH_MODE=local или both кто угодно читал и
менял чужой аккаунт запросом ?tg_id=<жертва>. Telegram ID не секрет, так что
это полный захват аккаунта из-за описки в конфиге.
"""
import asyncio
import pathlib
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api.auth import TelegramUser
from api.routers.users import _resolve_user_id


class _Req:
    """Минимальный запрос: важен только адрес собеседника по TCP."""

    def __init__(self, host):
        self.client = type("C", (), {"host": host})() if host else None


def _call(host, tg_user=None, tg_id=None):
    return asyncio.run(_resolve_user_id(_Req(host), tg_user, tg_id))


def test_signed_telegram_user_wins():
    user = TelegramUser(id=111, username="u", first_name="U", language_code="ru")
    # подписанные данные важнее любого tg_id в адресе — подменить себя нельзя
    assert _call("8.8.8.8", tg_user=user, tg_id=999) == 111


def test_local_dashboard_still_works():
    assert _call("127.0.0.1", tg_id=42) == 42
    assert _call("::1", tg_id=42) == 42


def test_remote_tg_id_is_refused():
    for host in ("8.8.8.8", "172.18.0.5", "2a00:1450::1"):
        with pytest.raises(HTTPException) as exc:
            _call(host, tg_id=42)
        assert exc.value.status_code == 401


def test_no_client_no_fallback():
    with pytest.raises(HTTPException):
        _call(None, tg_id=42)


def test_missing_tg_id_is_401():
    with pytest.raises(HTTPException) as exc:
        _call("127.0.0.1")
    assert exc.value.status_code == 401
