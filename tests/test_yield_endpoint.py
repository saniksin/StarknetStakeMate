"""End-to-end tests for ``GET /api/v1/users/me/yield-data``.

These exercise the FastAPI app via ``TestClient``. RPC fan-out and the
DB layer are monkeypatched so no network or sqlite work happens.

Conftest.py sets ``API_AUTH_MODE=local``, which lets us use the
``?tg_id=...`` query auth path (no Telegram WebApp HMAC required).
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import app
from services import yield_service


@pytest.fixture
def client() -> TestClient:
    """One TestClient per test — lifecycle hooks fire on enter/exit so
    the startup ABI warm-up is exercised in isolation."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Drop any cached yield payload between tests to keep them
    independent. Without this a test can see another test's payload
    when the user_id collides."""
    yield_service.invalidate_cache()
    yield


def _patch_db(monkeypatch, *, tracking_data: str | None = None) -> None:
    """Stub ``get_account`` so we don't need a real sqlite session."""
    fake_user = SimpleNamespace(
        user_id="999",
        user_name="alice",
        user_language="en",
        tracking_data=tracking_data,
    )

    async def _fake_get_account(_user_id: str):
        return fake_user

    monkeypatch.setattr("api.routers.users.get_account", _fake_get_account)


def _patch_yield(monkeypatch, *, entries: list, prices: dict) -> None:
    """Stub the yield service's two upstream dependencies."""

    async def _fake_fetch_entries(_tracking_data):
        return entries

    async def _fake_prices():
        return prices

    monkeypatch.setattr(yield_service, "fetch_tracking_entries", _fake_fetch_entries)
    monkeypatch.setattr(yield_service, "get_usd_prices", _fake_prices)


def _make_validator_entry(addr: str, label: str, commission_bps: int, pools: list[dict]):
    from services.staking_dto import PoolInfoDto, ValidatorInfo
    from services.tracking_service import TrackingEntry

    pool_dtos = [
        PoolInfoDto(
            pool_contract=p["pool_contract"],
            token_address=p["token_address"],
            token_symbol=p["symbol"],
            amount_raw=int(p["delegated"]),
            amount_decimal=Decimal(p["delegated"]) / (Decimal(10) ** p["decimals"]),
        )
        for p in pools
    ]
    own_raw = int(pools[0]["own"]) if pools else 0
    info = ValidatorInfo(
        staker_address=addr,
        reward_address=addr,
        operational_address=addr,
        amount_own_raw=own_raw,
        amount_own_strk=Decimal(own_raw) / Decimal(10**18),
        unclaimed_rewards_own_raw=0,
        unclaimed_rewards_own_strk=Decimal(0),
        commission_bps=commission_bps,
        pools=pool_dtos,
        current_epoch=100,
    )
    return TrackingEntry(
        index=0,
        kind="validator",
        address=addr,
        pool="0x0",
        label=label,
        data=info,
    )


def test_yield_data_requires_auth_when_no_tg_id(client) -> None:
    """Local-auth mode without a tg_id query param returns 401."""
    res = client.get("/api/v1/users/me/yield-data")
    assert res.status_code == 401, res.text


def test_yield_data_response_shape(client, monkeypatch) -> None:
    """Auth'd request returns the documented shape."""
    _patch_db(monkeypatch)
    _patch_yield(
        monkeypatch,
        entries=[
            _make_validator_entry(
                addr="0xVAL",
                label="anastasiia",
                commission_bps=1500,
                pools=[
                    {
                        "pool_contract": "0xPS",
                        "token_address": "0xSTRK",
                        "symbol": "STRK",
                        "decimals": 18,
                        "own": "37851000000000000000000",
                        "delegated": "2912149000000000000000000",
                    }
                ],
            )
        ],
        prices={"STRK": Decimal("0.05")},
    )

    res = client.get("/api/v1/users/me/yield-data?tg_id=999")
    assert res.status_code == 200, res.text
    body = res.json()

    assert "validators" in body
    assert "delegators" in body
    assert "stale" in body
    assert "fetched_at" in body
    assert body["stale"] is False

    assert len(body["validators"]) == 1
    v = body["validators"][0]
    assert v["address"] == "0xVAL"
    assert v["label"] == "anastasiia"
    assert v["commission_bps"] == 1500
    assert len(v["pools"]) == 1
    p = v["pools"][0]
    assert p["symbol"] == "STRK"
    assert p["decimals"] == 18
    # API contract: amounts as STRINGS (BigInt-safe).
    assert isinstance(p["own"], str)
    assert isinstance(p["delegated"], str)
    assert p["own"] == "37851000000000000000000"
    assert p["delegated"] == "2912149000000000000000000"
    assert Decimal(p["price_usd"]) == Decimal("0.05")


def test_yield_data_empty_user_returns_empty_lists(client, monkeypatch) -> None:
    _patch_db(monkeypatch, tracking_data=None)
    _patch_yield(monkeypatch, entries=[], prices={})

    res = client.get("/api/v1/users/me/yield-data?tg_id=1")
    assert res.status_code == 200
    body = res.json()
    assert body["validators"] == []
    assert body["delegators"] == []
    assert body["stale"] is False


def test_yield_data_uses_cache_within_ttl(client, monkeypatch) -> None:
    """Two consecutive GETs within 60s share one underlying RPC fan-out."""
    _patch_db(monkeypatch)

    call_count = {"n": 0}

    async def _fake_fetch_entries(_tracking_data):
        call_count["n"] += 1
        return []

    async def _fake_prices():
        return {}

    monkeypatch.setattr(yield_service, "fetch_tracking_entries", _fake_fetch_entries)
    monkeypatch.setattr(yield_service, "get_usd_prices", _fake_prices)

    res1 = client.get("/api/v1/users/me/yield-data?tg_id=999")
    res2 = client.get("/api/v1/users/me/yield-data?tg_id=999")
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert call_count["n"] == 1


def test_yield_data_unknown_user_returns_empty(client, monkeypatch) -> None:
    """When the DB has no row for the tg_id, we still respond 200 with
    empty lists — the Mini App can show the "no tracked yet" empty state
    without the user having to hit the bot first."""

    async def _fake_get_account(_user_id):
        return None

    monkeypatch.setattr("api.routers.users.get_account", _fake_get_account)
    _patch_yield(monkeypatch, entries=[], prices={})

    res = client.get("/api/v1/users/me/yield-data?tg_id=42")
    assert res.status_code == 200
    body = res.json()
    assert body["validators"] == []
    assert body["delegators"] == []
