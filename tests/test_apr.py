"""Network APR, computed from the staking contracts.

APR is emission over stake, and both sides are on chain. The number that
matters is the **gross** rate — before any validator commission — because
the Yield calculator applies commission itself; handing it a
post-commission figure would subtract the cut twice.

Checked against Endur's published mainnet number when this was written:
7.4825% both ways, to four decimals.
"""
from __future__ import annotations

import json

import pytest

from services import apr_service
from services.apr_service import fetch_network_apr, invalidate_apr_cache

# Mainnet, as read on 2026-09-23.
EPOCH_REWARDS_STRK = 13_962_753_000_000_000_000_000   # 13 962.753 STRK
EPOCH_REWARDS_BTC = 4_654_251_000_000_000_000_000     # 4 654.251 STRK to BTC pools
TOTAL_STAKED = 1_634_669_551 * 10**18
BTC_POWER = 586_188_396_200_288_689_859               # ~586 BTC, 18-dec scale
EPOCH_SECONDS = 3600
EXPECTED_STRK_APR = 7.4825


class _FakeContract:
    def __init__(self, reward_supplier: int) -> None:
        self.functions = {
            "contract_parameters_v1": _FakeFn({"reward_supplier": reward_supplier})
        }


class _FakeFn:
    def __init__(self, value):
        self._value = value

    async def call(self):
        return (self._value,)


def _install_chain(
    monkeypatch,
    *,
    rewards=(EPOCH_REWARDS_STRK, EPOCH_REWARDS_BTC),
    total=TOTAL_STAKED,
    power=(TOTAL_STAKED, BTC_POWER),
    epoch_seconds=EPOCH_SECONDS,
    prices=None,
    raise_on=None,
):
    """Stub the four chain reads the service makes."""
    import services.rpc_client as rpc
    import services.staking_service as staking

    monkeypatch.setattr(rpc, "get_client", lambda *_a, **_kw: object())
    monkeypatch.setattr(
        staking, "_staking_contract", lambda *_a, **_kw: _FakeContract(0xABC)
    )

    async def _epoch_info(*, network=None):  # noqa: ARG001
        return {"epoch_duration": epoch_seconds} if epoch_seconds else None

    monkeypatch.setattr(staking, "fetch_epoch_info", _epoch_info)

    async def _call(_client, address, selector):
        if raise_on == selector:
            raise RuntimeError("node is down")
        if selector == apr_service._TOTAL_STAKE_SELECTOR:
            return [total]
        if selector == apr_service._TOTAL_STAKING_POWER_SELECTOR:
            return list(power)
        if selector == apr_service._EPOCH_REWARDS_SELECTOR:
            return list(rewards)
        raise AssertionError(f"unexpected selector {selector:#x}")

    monkeypatch.setattr(apr_service, "_call", _call)

    import services.price_service as ps

    async def _prices():
        if prices is None:
            raise RuntimeError("no prices")
        return prices

    monkeypatch.setattr(ps, "get_usd_prices", _prices)


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(apr_service, "_STORE_PATH", tmp_path / "apr.json")
    invalidate_apr_cache()
    yield
    invalidate_apr_cache()


# ---------------------------------------------------------------------------
# The computation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_strk_apr_matches_the_published_figure(monkeypatch) -> None:
    _install_chain(monkeypatch)
    got = await fetch_network_apr("mainnet")
    assert got.status == "ok"
    assert got.strk_percent == pytest.approx(EXPECTED_STRK_APR, abs=1e-3)
    assert got.source == "chain"


@pytest.mark.asyncio
async def test_epoch_length_scales_the_rate(monkeypatch) -> None:
    """Sepolia runs 20-minute epochs against mainnet's hour, so the same
    per-epoch emission is three times the yearly rate."""
    _install_chain(monkeypatch, epoch_seconds=1200)
    got = await fetch_network_apr("mainnet")
    assert got.strk_percent == pytest.approx(EXPECTED_STRK_APR * 3, abs=1e-2)


@pytest.mark.asyncio
async def test_btc_apr_needs_both_prices(monkeypatch) -> None:
    _install_chain(monkeypatch, prices={"STRK": 0.0422, "WBTC": 85823})
    got = await fetch_network_apr("mainnet")
    assert got.btc_percent == pytest.approx(3.41, abs=0.05)


@pytest.mark.asyncio
async def test_btc_apr_is_absent_not_zero_without_prices(monkeypatch) -> None:
    """Zero would claim the BTC pools yield nothing, which is a different
    statement from "we can't price this"."""
    _install_chain(monkeypatch, prices=None)
    got = await fetch_network_apr("mainnet")
    assert got.status == "ok"
    assert got.strk_percent is not None      # STRK never depends on a price
    assert got.btc_percent is None


@pytest.mark.asyncio
async def test_missing_epoch_duration_is_unavailable(monkeypatch) -> None:
    _install_chain(monkeypatch, epoch_seconds=0)
    assert (await fetch_network_apr("mainnet")).status == "unavailable"


@pytest.mark.asyncio
async def test_zero_total_stake_is_unavailable(monkeypatch) -> None:
    # Division by zero would otherwise surface as a 500.
    _install_chain(monkeypatch, total=0)
    assert (await fetch_network_apr("mainnet")).status == "unavailable"


@pytest.mark.asyncio
async def test_node_failure_is_unavailable(monkeypatch) -> None:
    _install_chain(monkeypatch, raise_on=apr_service._EPOCH_REWARDS_SELECTOR)
    got = await fetch_network_apr("mainnet")
    assert got.status == "unavailable"
    assert got.detail


@pytest.mark.asyncio
async def test_absurd_rate_is_rejected(monkeypatch) -> None:
    # A rate over 1000% means the units changed upstream, not a windfall.
    _install_chain(monkeypatch, total=10**18)
    assert (await fetch_network_apr("mainnet")).status == "unavailable"


# ---------------------------------------------------------------------------
# Persistence across a dead upstream and a restart
# ---------------------------------------------------------------------------

def _patch_fetch(monkeypatch, result):
    async def _fake(_network):
        return result
    monkeypatch.setattr(apr_service, "_fetch", _fake)


def _good(network="mainnet", strk=EXPECTED_STRK_APR, btc=3.4):
    from services.staking_dto import NetworkApr
    return NetworkApr(
        network=network, status="ok", strk_percent=strk, btc_percent=btc,
        measured_at=None,
    )


@pytest.mark.asyncio
async def test_good_reading_is_persisted(monkeypatch) -> None:
    _patch_fetch(monkeypatch, _good())
    got = await fetch_network_apr("mainnet")
    assert got.status == "ok"
    saved = json.loads(apr_service._STORE_PATH.read_text())
    assert saved["mainnet"]["strk_percent"] == pytest.approx(EXPECTED_STRK_APR)


@pytest.mark.asyncio
async def test_dead_upstream_serves_the_last_known_figure(monkeypatch) -> None:
    """APR barely moves, so yesterday's real number beats a build-time
    constant — as long as the response says it is stale."""
    _patch_fetch(monkeypatch, _good())
    await fetch_network_apr("mainnet")

    invalidate_apr_cache()
    _patch_fetch(monkeypatch, apr_service._unavailable("mainnet", "upstream unreachable"))
    got = await fetch_network_apr("mainnet")

    assert got.status == "stale"
    assert got.strk_percent == pytest.approx(EXPECTED_STRK_APR)
    assert got.detail == "upstream unreachable"
    # The timestamp stays the ORIGINAL reading's, which is what makes the
    # staleness visible in the UI.
    assert got.measured_at is not None


@pytest.mark.asyncio
async def test_stored_figure_survives_a_process_restart(monkeypatch) -> None:
    _patch_fetch(monkeypatch, _good())
    await fetch_network_apr("mainnet")

    # Simulate a restart: process caches gone, only the file remains.
    invalidate_apr_cache()
    _patch_fetch(monkeypatch, apr_service._unavailable("mainnet", "down"))
    got = await fetch_network_apr("mainnet")
    assert got.status == "stale" and got.strk_percent == pytest.approx(EXPECTED_STRK_APR)


@pytest.mark.asyncio
async def test_networks_are_stored_independently(monkeypatch) -> None:
    _patch_fetch(monkeypatch, _good())
    await fetch_network_apr("mainnet")
    _patch_fetch(monkeypatch, _good("sepolia", 44.038, 0.0084))
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
    _patch_fetch(monkeypatch, _good())
    assert (await fetch_network_apr("mainnet")).status == "ok"
    assert json.loads(apr_service._STORE_PATH.read_text())["mainnet"]


@pytest.mark.asyncio
async def test_result_is_cached(monkeypatch) -> None:
    calls = {"n": 0}

    async def _counting(_network):
        calls["n"] += 1
        return _good()

    monkeypatch.setattr(apr_service, "_fetch", _counting)
    await fetch_network_apr("mainnet")
    await fetch_network_apr("mainnet")
    assert calls["n"] == 1
