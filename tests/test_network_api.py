"""API surface of the network selector.

``?network=`` is the only thing the Mini App sends to switch chains, so
these tests cover what happens for a good value, a bad one, and one we
don't serve — plus the routing that makes a testnet request actually
read the testnet list.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import app
from data.contracts import DEFAULT_NETWORK
from services import tracking_service

MAIN_ADDR = "0x" + "a" * 63
TEST_ADDR = "0x" + "b" * 63


@pytest.fixture
def client() -> TestClient:
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


def _patch_db(monkeypatch, tracking_data: str | None) -> SimpleNamespace:
    fake_user = SimpleNamespace(
        user_id="999",
        user_name="alice",
        user_language="en",
        tracking_data=tracking_data,
    )

    async def _fake_get_account(_user_id: str):
        return fake_user

    monkeypatch.setattr("api.routers.users.get_account", _fake_get_account)
    return fake_user


def _two_network_doc() -> str:
    doc = tracking_service.store_tracking(
        None, {"validators": [{"address": MAIN_ADDR, "label": "main"}], "delegations": []}
    )
    return tracking_service.store_tracking(
        doc,
        {"validators": [{"address": TEST_ADDR, "label": "test"}], "delegations": []},
        "sepolia",
    )


# ---------------------------------------------------------------------------
# GET /api/v1/networks
# ---------------------------------------------------------------------------

def test_networks_endpoint_lists_configured_chains(client) -> None:
    body = client.get("/api/v1/networks").json()
    assert body["default"] == DEFAULT_NETWORK
    assert body["networks"][0] == DEFAULT_NETWORK
    assert set(body["networks"]) == {"mainnet", "sepolia"}


# ---------------------------------------------------------------------------
# ?network= validation
# ---------------------------------------------------------------------------

def test_unknown_network_is_a_client_error(client, monkeypatch) -> None:
    _patch_db(monkeypatch, None)
    r = client.get("/api/v1/users/me/tracking?tg_id=999&network=goerli")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "unknown_network"


def test_unconfigured_network_is_service_unavailable(client, monkeypatch) -> None:
    """A valid chain with no endpoint is our missing config, not the
    caller's mistake — and the response says what we *do* serve so the
    Mini App can hide the tab instead of retrying."""
    import api.deps as deps

    monkeypatch.setattr(deps, "is_network_available", lambda _n: False)
    monkeypatch.setattr(deps, "available_networks", lambda: ["mainnet"])
    _patch_db(monkeypatch, None)

    r = client.get("/api/v1/users/me/tracking?tg_id=999&network=sepolia")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "network_unavailable"
    assert r.json()["detail"]["available"] == ["mainnet"]


# ---------------------------------------------------------------------------
# Routing: the parameter has to reach the storage layer
# ---------------------------------------------------------------------------

def test_tracking_list_is_scoped_to_the_requested_network(client, monkeypatch) -> None:
    _patch_db(monkeypatch, _two_network_doc())

    default = client.get("/api/v1/users/me/tracking?tg_id=999").json()
    mainnet = client.get("/api/v1/users/me/tracking?tg_id=999&network=mainnet").json()
    testnet = client.get("/api/v1/users/me/tracking?tg_id=999&network=testnet").json()

    assert [v["address"] for v in default["validators"]] == [MAIN_ADDR]
    assert [v["address"] for v in mainnet["validators"]] == [MAIN_ADDR]
    assert [v["address"] for v in testnet["validators"]] == [TEST_ADDR]


def test_replacing_the_testnet_list_leaves_mainnet_alone(client, monkeypatch) -> None:
    user = _patch_db(monkeypatch, _two_network_doc())

    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr("api.routers.users.write_to_db", _noop)
    monkeypatch.setattr("api.routers.users.clear_user_cache", _noop)

    r = client.put(
        "/api/v1/users/me/tracking?tg_id=999&network=sepolia",
        json={"validators": [], "delegations": []},
    )
    assert r.status_code == 200
    raw = json.loads(user.tracking_data)
    assert [v["address"] for v in raw["validators"]] == [MAIN_ADDR]
    assert not raw.get("networks")


def test_entries_endpoint_passes_the_network_downstream(client, monkeypatch) -> None:
    """``/entries`` fans out to RPC; assert the chain reaches that call
    rather than re-testing the storage layer."""
    seen: list = []

    async def _fake_fetch(tracking_data, network=None):
        seen.append(network)
        return []

    _patch_db(monkeypatch, _two_network_doc())
    monkeypatch.setattr("api.routers.users.fetch_tracking_entries", _fake_fetch)

    client.get("/api/v1/users/me/entries?tg_id=999&network=testnet")
    client.get("/api/v1/users/me/entries?tg_id=999")
    assert seen == ["sepolia", DEFAULT_NETWORK]


def test_status_node_probe_follows_the_network(client, monkeypatch) -> None:
    seen: list = []

    async def _fake_sync(*, network=None):
        seen.append(network)
        return None

    monkeypatch.setattr("api.routers.status.fetch_node_sync", _fake_sync)

    client.get("/api/v1/status/node?network=testnet")
    client.get("/api/v1/status/node")
    assert seen == ["sepolia", DEFAULT_NETWORK]


def test_attestation_alerts_are_saved_per_network(client, monkeypatch) -> None:
    """Subscribing on testnet must validate against the *testnet* tracked
    list and land in the testnet slice."""
    from db_api.models import Users

    user = Users(user_id=999, user_name="a", user_language="en", registration_data="n")
    user.tracking_data = _two_network_doc()

    async def _fake_get_account(_user_id: str):
        return user

    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr("api.routers.users.get_account", _fake_get_account)
    monkeypatch.setattr("services.attestation_prefs.write_to_db", _noop)

    r = client.put(
        "/api/v1/users/me/attestation-alerts?tg_id=999&network=testnet",
        json={"addresses": [TEST_ADDR]},
    )
    assert r.status_code == 200
    assert user.get_notification_config("sepolia")["attestation_alerts_for"] == [TEST_ADDR]
    assert user.get_notification_config()["attestation_alerts_for"] == []

    # The mainnet validator is not tracked on testnet, so subscribing to
    # it there is rejected rather than silently stored.
    bad = client.put(
        "/api/v1/users/me/attestation-alerts?tg_id=999&network=testnet",
        json={"addresses": [MAIN_ADDR]},
    )
    assert bad.status_code == 400
