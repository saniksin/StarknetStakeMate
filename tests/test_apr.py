"""Network APR: picking the gross rate, and surviving a dead upstream.

The Yield calculator applies validator commission itself, so it must be
fed the rate *before* any commission. Endur quotes ``apy`` already net of
each validator's cut, which makes the zero-commission rows the gross
figure — reading the wrong ones would double-count the commission and
silently understate every delegator's yield.
"""
from __future__ import annotations

import json

import pytest

from services import apr_service
from services.apr_service import _pick, fetch_network_apr, invalidate_apr_cache

# Mainnet numbers as published: gross 7.4825 / 3.3970, and every tier
# reconstructs to four decimals via apy = gross * (1 - commission).
GROSS_STRK = 7.4825
GROSS_BTC = 3.3970


def _row(commission, *, active=True, apy=None, btc=None, updated="2026-09-23T10:40:02.501Z"):
    c = float(commission)
    return {
        "commission": str(commission),
        "is_active": active,
        "apy": GROSS_STRK * (1 - c / 100) if apy is None else apy,
        "btc_apy": GROSS_BTC * (1 - c / 100) if btc is None else btc,
        "updated_at": updated,
    }


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(apr_service, "_STORE_PATH", tmp_path / "apr.json")
    invalidate_apr_cache()
    yield
    invalidate_apr_cache()


# ---------------------------------------------------------------------------
# Picking the gross rate
# ---------------------------------------------------------------------------

def test_reads_the_zero_commission_validators() -> None:
    got = _pick([_row(0), _row(0), _row(10), _row(15)], "mainnet")
    assert got.status == "ok"
    assert got.strk_percent == pytest.approx(GROSS_STRK)
    assert got.btc_percent == pytest.approx(GROSS_BTC)
    assert got.derived is False
    assert got.sample_size == 2
    assert got.measured_at is not None


def test_a_commissioned_validator_is_never_mistaken_for_gross() -> None:
    """The bug this guards: taking any validator's apy would hand the
    calculator a post-commission rate, and it would subtract commission a
    second time."""
    got = _pick([_row(15), _row(10)], "mainnet")
    assert got.strk_percent == pytest.approx(GROSS_STRK)   # not 6.36
    assert got.derived is True                             # and flagged as such


def test_median_ignores_one_odd_row() -> None:
    rows = [_row(0), _row(0), _row(0, apy=999.0)]
    got = _pick(rows, "mainnet")
    assert got.strk_percent == pytest.approx(GROSS_STRK)


def test_inactive_validators_are_ignored() -> None:
    # Inactive rows report apy 0 and would drag a mean to nonsense.
    rows = [_row(0, active=False, apy=0, btc=0), _row(5)]
    got = _pick(rows, "mainnet")
    assert got.status == "ok"
    assert got.strk_percent == pytest.approx(GROSS_STRK)


def test_zero_btc_apy_is_kept_not_treated_as_missing() -> None:
    """A network with no BTC pools genuinely yields 0 there; replacing it
    with a built-in constant would invent a yield that doesn't exist."""
    got = _pick([_row(0, btc=0.0)], "sepolia")
    assert got.status == "ok" and got.btc_percent == 0.0


def test_absurd_rate_is_rejected() -> None:
    got = _pick([_row(0, apy=100000.0)], "mainnet")
    assert got.status == "unavailable"


def test_no_active_validators_is_unavailable() -> None:
    got = _pick([_row(0, active=False)], "mainnet")
    assert got.status == "unavailable"


# ---------------------------------------------------------------------------
# Persistence across a dead upstream and a restart
# ---------------------------------------------------------------------------

def _patch_fetch(monkeypatch, result):
    async def _fake(_network):
        return result
    monkeypatch.setattr(apr_service, "_fetch", _fake)


@pytest.mark.asyncio
async def test_good_reading_is_persisted(monkeypatch) -> None:
    _patch_fetch(monkeypatch, _pick([_row(0)], "mainnet"))
    got = await fetch_network_apr("mainnet")
    assert got.status == "ok"
    saved = json.loads(apr_service._STORE_PATH.read_text())
    assert saved["mainnet"]["strk_percent"] == pytest.approx(GROSS_STRK)


@pytest.mark.asyncio
async def test_dead_upstream_serves_the_last_known_figure(monkeypatch) -> None:
    """APR barely moves, so yesterday's real number beats a build-time
    constant — as long as the response says it is stale."""
    _patch_fetch(monkeypatch, _pick([_row(0)], "mainnet"))
    await fetch_network_apr("mainnet")

    invalidate_apr_cache()
    _patch_fetch(monkeypatch, apr_service._unavailable("mainnet", "upstream unreachable"))
    got = await fetch_network_apr("mainnet")

    assert got.status == "stale"
    assert got.strk_percent == pytest.approx(GROSS_STRK)
    assert got.detail == "upstream unreachable"
    # The timestamp stays the ORIGINAL reading's, which is what makes the
    # staleness visible in the UI.
    assert got.measured_at is not None


@pytest.mark.asyncio
async def test_stored_figure_survives_a_process_restart(monkeypatch) -> None:
    _patch_fetch(monkeypatch, _pick([_row(0)], "mainnet"))
    await fetch_network_apr("mainnet")

    # Simulate a restart: process caches gone, only the file remains.
    invalidate_apr_cache()
    _patch_fetch(monkeypatch, apr_service._unavailable("mainnet", "down"))
    got = await fetch_network_apr("mainnet")
    assert got.status == "stale" and got.strk_percent == pytest.approx(GROSS_STRK)


@pytest.mark.asyncio
async def test_networks_are_stored_independently(monkeypatch) -> None:
    _patch_fetch(monkeypatch, _pick([_row(0)], "mainnet"))
    await fetch_network_apr("mainnet")
    _patch_fetch(monkeypatch, _pick([_row(0, apy=44.038, btc=0.0084)], "sepolia"))
    await fetch_network_apr("sepolia")

    invalidate_apr_cache()
    _patch_fetch(monkeypatch, apr_service._unavailable("sepolia", "down"))
    got = await fetch_network_apr("sepolia")
    assert got.strk_percent == pytest.approx(44.038)

    saved = json.loads(apr_service._STORE_PATH.read_text())
    assert set(saved) == {"mainnet", "sepolia"}


@pytest.mark.asyncio
async def test_no_stored_figure_yields_unavailable(monkeypatch) -> None:
    _patch_fetch(monkeypatch, apr_service._unavailable("mainnet", "down"))
    got = await fetch_network_apr("mainnet")
    assert got.status == "unavailable"


@pytest.mark.asyncio
async def test_corrupt_store_does_not_break_the_endpoint(monkeypatch) -> None:
    apr_service._STORE_PATH.write_text("{ this is not json")
    _patch_fetch(monkeypatch, apr_service._unavailable("mainnet", "down"))
    got = await fetch_network_apr("mainnet")
    assert got.status == "unavailable"

    # ...and a later good reading repairs it.
    invalidate_apr_cache()
    _patch_fetch(monkeypatch, _pick([_row(0)], "mainnet"))
    assert (await fetch_network_apr("mainnet")).status == "ok"
    assert json.loads(apr_service._STORE_PATH.read_text())["mainnet"]


@pytest.mark.asyncio
async def test_result_is_cached(monkeypatch) -> None:
    calls = {"n": 0}

    async def _counting(_network):
        calls["n"] += 1
        return _pick([_row(0)], "mainnet")

    monkeypatch.setattr(apr_service, "_fetch", _counting)
    await fetch_network_apr("mainnet")
    await fetch_network_apr("mainnet")
    assert calls["n"] == 1
