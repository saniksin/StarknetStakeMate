"""Validator uptime from Endur's index.

The contract this module has to keep is narrow but strict:

  * every call returns a DTO — never ``None``, never an exception — so a
    third party changing their API surfaces as a visible "couldn't fetch
    this" rather than a block that quietly stops rendering;
  * an address is normalised to the one spelling their route accepts,
    because ``0x1`` and sixty-three zeros plus ``1`` are the same address
    and users paste whichever their tooling printed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services import uptime_service
from services.uptime_service import (
    fetch_validator_uptime,
    invalidate_uptime_cache,
    normalize_validator_address,
)

ADDR = "0x0475a1ba31db59f0eda3b3b260ad3abb30a2a67983cd51d753fdb4adad92a524"

_PAYLOAD = {
    "address": ADDR,
    "liveliness": 99.5,
    "is_active": True,
    "is_unstaking": False,
    "name": "Saniksin",
    "logo": "https://assets.endur.fi/validators/saniksin.png",
    "active_since": "2024-11-26T14:46:01.000Z",
    "updated_at": "2026-09-23T10:09:56.295Z",
}


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_uptime_cache()
    yield
    invalidate_uptime_cache()


# ---------------------------------------------------------------------------
# Address normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spelling",
    [
        ADDR,                                   # canonical, 66 chars
        ADDR.replace("0x0", "0x", 1),           # no leading zero
        "0x" + "0" * 6 + ADDR[2:].lstrip("0"),  # extra leading zeros
        ADDR.upper().replace("0X", "0x", 1),    # upper-case body
        f"  {ADDR}  ",                          # stray whitespace
    ],
)
def test_every_spelling_of_one_address_normalises_the_same(spelling) -> None:
    """Endur's route answers HTTP 500 for anything but the 66-char
    lower-case form, so this is the difference between working and not."""
    assert normalize_validator_address(spelling) == ADDR
    assert len(normalize_validator_address(spelling)) == 66


@pytest.mark.parametrize("bad", ["", None, "not-an-address", "0xzz", "hello"])
def test_non_addresses_are_rejected(bad) -> None:
    assert normalize_validator_address(bad) is None


def test_oversized_felt_is_rejected() -> None:
    # A felt is < 2^252; anything larger cannot be an address.
    assert normalize_validator_address("0x" + "f" * 64) is None


# ---------------------------------------------------------------------------
# Result states
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, body, text: str = "") -> None:
        self.status = status
        self._body = body
        self._text = text or ""

    async def text(self) -> str:
        return self._text

    async def json(self, content_type=None):  # noqa: ARG002
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    def __init__(self, response=None, raise_exc: BaseException | None = None) -> None:
        self._response = response
        self._raise = raise_exc

    def get(self, *_a, **_kw):
        if self._raise is not None:
            raise self._raise
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _patch_session(monkeypatch, session) -> None:
    monkeypatch.setattr(
        uptime_service.aiohttp, "ClientSession", lambda *a, **kw: session
    )


@pytest.mark.asyncio
async def test_ok_result_carries_the_percentage_and_provenance(monkeypatch) -> None:
    _patch_session(monkeypatch, _FakeSession(_FakeResponse(200, _PAYLOAD)))
    u = await fetch_validator_uptime(ADDR)
    assert u.status == "ok"
    assert u.percent == 99.5
    assert u.name == "Saniksin"
    assert u.is_active is True
    # Freshness is part of the answer: a stale 100% must not look fresh.
    assert u.measured_at is not None
    assert u.source == "endur"


@pytest.mark.asyncio
async def test_unknown_validator_is_not_indexed_not_an_error(monkeypatch) -> None:
    """Their API says "not found" with HTTP 500, so the status code alone
    would misclassify a perfectly normal answer as an outage."""
    _patch_session(
        monkeypatch,
        _FakeSession(_FakeResponse(500, None, '{"error":{"message":"Validator not found"}}')),
    )
    u = await fetch_validator_uptime(ADDR)
    assert u.status == "not_indexed"
    assert u.percent is None


@pytest.mark.asyncio
async def test_upstream_down_is_visible_not_silent(monkeypatch) -> None:
    _patch_session(
        monkeypatch,
        _FakeSession(raise_exc=uptime_service.aiohttp.ClientError("boom")),
    )
    u = await fetch_validator_uptime(ADDR)
    assert u.status == "unavailable"
    assert u.detail  # something for the tooltip
    assert u.percent is None


@pytest.mark.asyncio
async def test_schema_change_is_reported_rather_than_swallowed(monkeypatch) -> None:
    """If Endur drops or renames ``liveliness`` we must say so — that is
    the whole reason the block renders an explicit failure state."""
    _patch_session(
        monkeypatch,
        _FakeSession(_FakeResponse(200, {"address": ADDR, "is_active": True})),
    )
    u = await fetch_validator_uptime(ADDR)
    assert u.status == "unavailable"
    assert "liveliness" in (u.detail or "")


@pytest.mark.asyncio
async def test_non_numeric_liveliness_is_reported(monkeypatch) -> None:
    _patch_session(
        monkeypatch, _FakeSession(_FakeResponse(200, {**_PAYLOAD, "liveliness": "n/a"}))
    )
    u = await fetch_validator_uptime(ADDR)
    assert u.status == "unavailable"


@pytest.mark.asyncio
async def test_out_of_range_percentage_is_clamped(monkeypatch) -> None:
    _patch_session(
        monkeypatch, _FakeSession(_FakeResponse(200, {**_PAYLOAD, "liveliness": 140}))
    )
    u = await fetch_validator_uptime(ADDR)
    assert u.status == "ok" and u.percent == 100.0


@pytest.mark.asyncio
async def test_malformed_address_never_reaches_the_network(monkeypatch) -> None:
    def _boom(*_a, **_kw):
        raise AssertionError("should not have made a request")

    monkeypatch.setattr(uptime_service.aiohttp, "ClientSession", _boom)
    u = await fetch_validator_uptime("definitely not hex")
    assert u.status == "unavailable"


@pytest.mark.asyncio
async def test_only_https_logos_are_passed_through(monkeypatch) -> None:
    """The URL lands in an <img> src, so a javascript: or http: value has
    no business getting there."""
    _patch_session(
        monkeypatch,
        _FakeSession(_FakeResponse(200, {**_PAYLOAD, "logo": "javascript:alert(1)"})),
    )
    u = await fetch_validator_uptime(ADDR)
    assert u.logo_url is None


@pytest.mark.asyncio
async def test_result_is_cached_per_network(monkeypatch) -> None:
    calls = {"n": 0}

    class _CountingSession(_FakeSession):
        def get(self, *a, **kw):
            calls["n"] += 1
            return super().get(*a, **kw)

    session = _CountingSession(_FakeResponse(200, _PAYLOAD))
    _patch_session(monkeypatch, session)

    await fetch_validator_uptime(ADDR)
    await fetch_validator_uptime(ADDR)
    # Same address, different spelling — must hit the same cache entry.
    await fetch_validator_uptime(ADDR.replace("0x0", "0x", 1))
    assert calls["n"] == 1

    # A different network is a different key.
    await fetch_validator_uptime(ADDR, network="sepolia")
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_failures_expire_sooner_than_successes(monkeypatch) -> None:
    """A brief outage must not freeze the card into an error for the full
    success TTL after the upstream recovers."""
    monkeypatch.setattr(uptime_service, "_FAILURE_TTL_SECONDS", 0)
    _patch_session(
        monkeypatch,
        _FakeSession(raise_exc=uptime_service.aiohttp.ClientError("down")),
    )
    assert (await fetch_validator_uptime(ADDR)).status == "unavailable"

    _patch_session(monkeypatch, _FakeSession(_FakeResponse(200, _PAYLOAD)))
    assert (await fetch_validator_uptime(ADDR)).status == "ok"
