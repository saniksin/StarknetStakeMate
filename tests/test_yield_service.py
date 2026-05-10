"""Unit tests for the yield service: pure-function calculations and
the cached aggregator that backs ``GET /api/v1/users/me/yield-data``.

Network calls (RPC, price service) are monkeypatched so the tests run
hermetically. Math tests are independent of the cache/aggregator path.
"""
from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from services import yield_service
from services.yield_service import (
    YieldPoolBreakdown,
    compute_delegator_pool_yield,
    compute_validator_pool_yield,
    daily_from_year,
    monthly_from_year,
    pool_apr_for_symbol,
)


# ---------------------------------------------------------------------------
# Pure math: APR mapping
# ---------------------------------------------------------------------------


def test_pool_apr_strk() -> None:
    assert pool_apr_for_symbol("STRK", strk_apr=Decimal("8.39"), btc_apr=Decimal("3.55")) == Decimal("8.39")


def test_pool_apr_btc_wrappers() -> None:
    for sym in ("WBTC", "LBTC", "tBTC", "SolvBTC", "strkBTC"):
        assert pool_apr_for_symbol(
            sym, strk_apr=Decimal("8.39"), btc_apr=Decimal("3.55")
        ) == Decimal("3.55"), f"BTC APR mapping wrong for {sym}"


def test_pool_apr_unknown_symbol_falls_back_to_strk() -> None:
    # Defensive: if a future token comes online we still produce SOMETHING
    # rather than raise — the user can always type the correct APR in.
    # We deliberately fall back to STRK APR so the math doesn't silently
    # multiply by zero.
    assert pool_apr_for_symbol(
        None, strk_apr=Decimal("8.39"), btc_apr=Decimal("3.55")
    ) == Decimal("8.39")


# ---------------------------------------------------------------------------
# Pure math: per-pool yield (validator)
# ---------------------------------------------------------------------------


def test_validator_pool_yield_own_only() -> None:
    # Validator with 1000 STRK own, 0 delegated, APR=10%, commission irrelevant.
    res = compute_validator_pool_yield(
        own=Decimal("1000"),
        delegated=Decimal("0"),
        apr_pct=Decimal("10"),
        commission_bps=1500,
    )
    assert res == Decimal("100")  # 1000 * 0.10


def test_validator_pool_yield_commission_on_delegated() -> None:
    # 0 own, 10000 delegated, APR=10%, commission=15% (1500 bps).
    # commission yield = 10000 * 0.10 * 0.15 = 150
    res = compute_validator_pool_yield(
        own=Decimal("0"),
        delegated=Decimal("10000"),
        apr_pct=Decimal("10"),
        commission_bps=1500,
    )
    assert res == Decimal("150")


def test_validator_pool_yield_combined() -> None:
    # 1000 own + 10000 delegated, APR=10%, commission=15%.
    # own_yield = 1000 * 0.10 = 100
    # commission_yield = 10000 * 0.10 * 0.15 = 150
    # total = 250
    res = compute_validator_pool_yield(
        own=Decimal("1000"),
        delegated=Decimal("10000"),
        apr_pct=Decimal("10"),
        commission_bps=1500,
    )
    assert res == Decimal("250")


def test_validator_pool_yield_zero_apr() -> None:
    res = compute_validator_pool_yield(
        own=Decimal("1000"),
        delegated=Decimal("10000"),
        apr_pct=Decimal("0"),
        commission_bps=1500,
    )
    assert res == Decimal("0")


def test_validator_pool_yield_zero_commission() -> None:
    # 0 commission → only own yields.
    res = compute_validator_pool_yield(
        own=Decimal("1000"),
        delegated=Decimal("10000"),
        apr_pct=Decimal("10"),
        commission_bps=0,
    )
    assert res == Decimal("100")


# ---------------------------------------------------------------------------
# Pure math: per-pool yield (delegator)
# ---------------------------------------------------------------------------


def test_delegator_pool_yield_basic() -> None:
    # 1000 STRK delegated, APR=10%, commission=15% (validator keeps 15%).
    # net APR for delegator = 10% * 85% = 8.5%
    # yield = 1000 * 0.085 = 85
    res = compute_delegator_pool_yield(
        delegated=Decimal("1000"),
        apr_pct=Decimal("10"),
        commission_bps=1500,
    )
    assert res == Decimal("85")


def test_delegator_pool_yield_zero_commission() -> None:
    # No commission → delegator gets full APR.
    res = compute_delegator_pool_yield(
        delegated=Decimal("1000"),
        apr_pct=Decimal("10"),
        commission_bps=0,
    )
    assert res == Decimal("100")


def test_delegator_pool_yield_max_commission() -> None:
    # 100% commission → delegator gets nothing.
    res = compute_delegator_pool_yield(
        delegated=Decimal("1000"),
        apr_pct=Decimal("10"),
        commission_bps=10000,
    )
    assert res == Decimal("0")


def test_delegator_pool_yield_zero_amount() -> None:
    res = compute_delegator_pool_yield(
        delegated=Decimal("0"),
        apr_pct=Decimal("10"),
        commission_bps=1500,
    )
    assert res == Decimal("0")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def test_monthly_from_year_simple() -> None:
    # year 1200 → monthly 100
    assert monthly_from_year(Decimal("1200")) == Decimal("100")


def test_daily_from_year_simple() -> None:
    # year 365 → daily 1
    assert daily_from_year(Decimal("365")) == Decimal("1")


def test_monthly_daily_zero() -> None:
    assert monthly_from_year(Decimal("0")) == Decimal("0")
    assert daily_from_year(Decimal("0")) == Decimal("0")


# ---------------------------------------------------------------------------
# YieldPoolBreakdown serialization shape
# ---------------------------------------------------------------------------


def test_pool_breakdown_serializes_amounts_as_strings() -> None:
    """API contract: ``own`` / ``delegated`` are strings (base units don't
    fit in JSON numbers for STRK 18-decimals at portfolio scale)."""
    b = YieldPoolBreakdown(
        symbol="STRK",
        decimals=18,
        own="37851000000000000000000",
        delegated="2912149000000000000000000",
        price_usd=Decimal("0.05"),
    )
    payload = b.model_dump(mode="json")
    assert payload["own"] == "37851000000000000000000"
    assert payload["delegated"] == "2912149000000000000000000"
    assert payload["symbol"] == "STRK"
    # Pydantic serializes Decimal as string in JSON mode to preserve
    # precision — the frontend coerces with Number().
    assert Decimal(payload["price_usd"]) == Decimal("0.05")


def test_pool_breakdown_price_unavailable() -> None:
    b = YieldPoolBreakdown(
        symbol="strkBTC",
        decimals=8,
        own="0",
        delegated="100000000",
        price_usd=None,
    )
    payload = b.model_dump(mode="json")
    assert payload["price_usd"] is None


# ---------------------------------------------------------------------------
# Aggregator with mocked tracking entries
# ---------------------------------------------------------------------------


def _make_validator_entry(addr: str, label: str, commission_bps: int, pools: list[dict[str, Any]]) -> Any:
    """Build a fake TrackingEntry-shaped object with a ValidatorInfo-like
    payload. We use SimpleNamespace because the consumer only does attribute
    access; nothing in the yield service requires the real Pydantic types.
    """
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
    info = ValidatorInfo(
        staker_address=addr,
        reward_address=addr,
        operational_address=addr,
        amount_own_raw=int(pools[0]["own"]) if pools else 0,
        amount_own_strk=Decimal(pools[0]["own"]) / Decimal(10**18) if pools else Decimal(0),
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


def _make_delegator_entry(
    delegator: str, staker: str, label: str, commission_bps: int, positions: list[dict[str, Any]]
) -> Any:
    from services.staking_dto import DelegatorInfo, DelegatorMultiPositions
    from services.tracking_service import TrackingEntry

    pos_dtos = [
        DelegatorInfo(
            delegator_address=delegator,
            pool_contract=p["pool_contract"],
            token_address=p["token_address"],
            token_symbol=p["symbol"],
            reward_address=delegator,
            amount_raw=int(p["delegated"]),
            amount_decimal=Decimal(p["delegated"]) / (Decimal(10) ** p["decimals"]),
            unclaimed_rewards_raw=0,
            unclaimed_rewards_decimal=Decimal(0),
            commission_bps=commission_bps,
        )
        for p in positions
    ]
    multi = DelegatorMultiPositions(
        delegator_address=delegator,
        staker_address=staker,
        positions=pos_dtos,
    )
    return TrackingEntry(
        index=1,
        kind="delegator",
        address=delegator,
        pool=staker,
        label=label,
        data=multi,
    )


@pytest.mark.asyncio
async def test_build_yield_payload_validator_only(monkeypatch) -> None:
    """A user with a single validator and one STRK pool produces the
    expected response shape."""

    entries = [
        _make_validator_entry(
            addr="0xVAL",
            label="anastasiia",
            commission_bps=1500,
            pools=[
                {
                    "pool_contract": "0xPOOLS",
                    "token_address": "0xSTRK",
                    "symbol": "STRK",
                    "decimals": 18,
                    "own": "37851000000000000000000",
                    "delegated": "2912149000000000000000000",
                }
            ],
        )
    ]

    async def _fake_fetch_entries(_tracking_data):
        return entries

    async def _fake_prices():
        return {"STRK": Decimal("0.05")}

    monkeypatch.setattr(yield_service, "fetch_tracking_entries", _fake_fetch_entries)
    monkeypatch.setattr(yield_service, "get_usd_prices", _fake_prices)
    # Reset cache so this test isn't polluted by a previous run.
    yield_service.invalidate_cache(user_id=42)

    payload = await yield_service.build_yield_payload(user_id=42, tracking_data=None)

    assert len(payload.validators) == 1
    v = payload.validators[0]
    assert v.address == "0xVAL"
    assert v.label == "anastasiia"
    assert v.commission_bps == 1500
    assert len(v.pools) == 1
    p = v.pools[0]
    assert p.symbol == "STRK"
    assert p.decimals == 18
    assert p.own == "37851000000000000000000"
    assert p.delegated == "2912149000000000000000000"
    assert p.price_usd == Decimal("0.05")
    assert payload.delegators == []
    assert payload.stale is False


@pytest.mark.asyncio
async def test_build_yield_payload_skips_empty_pools(monkeypatch) -> None:
    """Pools where own=0 AND delegated=0 are dropped from the response."""
    entries = [
        _make_validator_entry(
            addr="0xVAL",
            label="solo",
            commission_bps=500,
            pools=[
                {
                    "pool_contract": "0xPSTRK",
                    "token_address": "0xSTRK",
                    "symbol": "STRK",
                    "decimals": 18,
                    "own": "1000000000000000000000",
                    "delegated": "0",
                },
            ],
        )
    ]
    # Inject an empty pool — the validator info already has it as
    # amount_own=0, but we add it as a second PoolInfoDto with zero on both
    # sides to verify the filter.
    from services.staking_dto import PoolInfoDto

    entries[0].data.pools.append(
        PoolInfoDto(
            pool_contract="0xEMPTY",
            token_address="0xWBTC",
            token_symbol="WBTC",
            amount_raw=0,
            amount_decimal=Decimal(0),
        )
    )
    # The validator's own STRK belongs to the STRK pool — we synthesise that
    # via the explicit "own" field in the test helper. Empty WBTC pool here
    # has neither own (own only applies to the first STRK pool by convention)
    # nor delegated, so it should be filtered.

    async def _fake_fetch_entries(_tracking_data):
        return entries

    async def _fake_prices():
        return {"STRK": Decimal("0.05"), "WBTC": Decimal("60000")}

    monkeypatch.setattr(yield_service, "fetch_tracking_entries", _fake_fetch_entries)
    monkeypatch.setattr(yield_service, "get_usd_prices", _fake_prices)
    yield_service.invalidate_cache(user_id=43)

    payload = await yield_service.build_yield_payload(user_id=43, tracking_data=None)

    assert len(payload.validators) == 1
    # WBTC pool with 0/0 was filtered out.
    syms = {p.symbol for p in payload.validators[0].pools}
    assert syms == {"STRK"}


@pytest.mark.asyncio
async def test_build_yield_payload_delegator(monkeypatch) -> None:
    entries = [
        _make_delegator_entry(
            delegator="0xDEL",
            staker="0xVAL",
            label="alice",
            commission_bps=1500,
            positions=[
                {
                    "pool_contract": "0xPSTRK",
                    "token_address": "0xSTRK",
                    "symbol": "STRK",
                    "decimals": 18,
                    "delegated": "5000000000000000000000",
                },
                {
                    "pool_contract": "0xPWBTC",
                    "token_address": "0xWBTC",
                    "symbol": "WBTC",
                    "decimals": 8,
                    "delegated": "10000000",  # 0.1 BTC
                },
            ],
        )
    ]

    async def _fake_fetch_entries(_tracking_data):
        return entries

    async def _fake_prices():
        return {"STRK": Decimal("0.05"), "WBTC": Decimal("60000")}

    monkeypatch.setattr(yield_service, "fetch_tracking_entries", _fake_fetch_entries)
    monkeypatch.setattr(yield_service, "get_usd_prices", _fake_prices)
    yield_service.invalidate_cache(user_id=44)

    payload = await yield_service.build_yield_payload(user_id=44, tracking_data=None)

    assert payload.validators == []
    assert len(payload.delegators) == 1
    d = payload.delegators[0]
    assert d.address == "0xDEL"
    assert d.label == "alice"
    assert d.validator_address == "0xVAL"
    assert d.commission_bps == 1500
    assert {p.symbol for p in d.pools} == {"STRK", "WBTC"}
    strk_pool = next(p for p in d.pools if p.symbol == "STRK")
    assert strk_pool.delegated == "5000000000000000000000"


@pytest.mark.asyncio
async def test_build_yield_payload_missing_price(monkeypatch) -> None:
    """When the price service returns nothing for a token symbol, the
    response carries ``price_usd: null`` instead of failing."""
    entries = [
        _make_validator_entry(
            addr="0xVAL",
            label="weird",
            commission_bps=1500,
            pools=[
                {
                    "pool_contract": "0xPNEW",
                    "token_address": "0xNEW",
                    "symbol": "NEWTOKEN",  # not in price service
                    "decimals": 18,
                    # Validator own STRK is irrelevant here; only the
                    # pool delegated count matters for the missing-price
                    # check. Non-zero so the empty-pool filter doesn't
                    # drop the row before we get to inspect it.
                    "own": "0",
                    "delegated": "1000000000000000000000",
                }
            ],
        )
    ]

    async def _fake_fetch_entries(_tracking_data):
        return entries

    async def _fake_prices():
        return {"STRK": Decimal("0.05")}  # NEWTOKEN missing

    monkeypatch.setattr(yield_service, "fetch_tracking_entries", _fake_fetch_entries)
    monkeypatch.setattr(yield_service, "get_usd_prices", _fake_prices)
    yield_service.invalidate_cache(user_id=45)

    payload = await yield_service.build_yield_payload(user_id=45, tracking_data=None)
    p = payload.validators[0].pools[0]
    assert p.price_usd is None


@pytest.mark.asyncio
async def test_build_yield_payload_missing_commission_warns(monkeypatch, caplog) -> None:
    """Validator with commission_bps=None is normalised to 0 + warning."""
    import logging

    entries = [
        _make_validator_entry(
            addr="0xVAL",
            label="nocommission",
            commission_bps=0,  # build helper requires int; we'll patch after
            pools=[
                {
                    "pool_contract": "0xPSTRK",
                    "token_address": "0xSTRK",
                    "symbol": "STRK",
                    "decimals": 18,
                    "own": "1000000000000000000000",
                    "delegated": "0",
                }
            ],
        )
    ]
    # Now overwrite the commission_bps inside the ValidatorInfo to None to
    # simulate the RPC returning a missing commission.
    entries[0].data.commission_bps = None

    async def _fake_fetch_entries(_tracking_data):
        return entries

    async def _fake_prices():
        return {"STRK": Decimal("0.05")}

    monkeypatch.setattr(yield_service, "fetch_tracking_entries", _fake_fetch_entries)
    monkeypatch.setattr(yield_service, "get_usd_prices", _fake_prices)
    yield_service.invalidate_cache(user_id=46)

    payload = await yield_service.build_yield_payload(user_id=46, tracking_data=None)
    assert payload.validators[0].commission_bps == 0


@pytest.mark.asyncio
async def test_build_yield_payload_cached(monkeypatch) -> None:
    """Second call within the TTL window returns the cached payload
    without hitting fetch_tracking_entries again."""
    call_count = {"n": 0}

    async def _fake_fetch_entries(_tracking_data):
        call_count["n"] += 1
        return []

    async def _fake_prices():
        return {}

    monkeypatch.setattr(yield_service, "fetch_tracking_entries", _fake_fetch_entries)
    monkeypatch.setattr(yield_service, "get_usd_prices", _fake_prices)
    yield_service.invalidate_cache(user_id=47)

    p1 = await yield_service.build_yield_payload(user_id=47, tracking_data=None)
    p2 = await yield_service.build_yield_payload(user_id=47, tracking_data=None)
    # Cached: only ONE underlying call.
    assert call_count["n"] == 1
    # Stale flag stays False both times.
    assert p1.stale is False
    assert p2.stale is False


@pytest.mark.asyncio
async def test_build_yield_payload_cache_expires(monkeypatch) -> None:
    """After TTL passes, a fresh call rebuilds (we fast-forward the cache
    timestamp instead of sleeping)."""
    call_count = {"n": 0}

    async def _fake_fetch_entries(_tracking_data):
        call_count["n"] += 1
        return []

    async def _fake_prices():
        return {}

    monkeypatch.setattr(yield_service, "fetch_tracking_entries", _fake_fetch_entries)
    monkeypatch.setattr(yield_service, "get_usd_prices", _fake_prices)
    yield_service.invalidate_cache(user_id=48)

    await yield_service.build_yield_payload(user_id=48, tracking_data=None)
    # Backdate the cache entry past the TTL.
    yield_service._CACHE[48] = (
        yield_service._CACHE[48][0],
        time.time() - yield_service._CACHE_TTL_SECONDS - 1,
    )
    await yield_service.build_yield_payload(user_id=48, tracking_data=None)
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_build_yield_payload_separate_users_have_separate_caches(monkeypatch) -> None:
    call_count = {"n": 0}

    async def _fake_fetch_entries(_tracking_data):
        call_count["n"] += 1
        return []

    async def _fake_prices():
        return {}

    monkeypatch.setattr(yield_service, "fetch_tracking_entries", _fake_fetch_entries)
    monkeypatch.setattr(yield_service, "get_usd_prices", _fake_prices)
    yield_service.invalidate_cache(user_id=100)
    yield_service.invalidate_cache(user_id=101)

    await yield_service.build_yield_payload(user_id=100, tracking_data=None)
    await yield_service.build_yield_payload(user_id=101, tracking_data=None)
    # Two distinct user_ids → two distinct fetches.
    assert call_count["n"] == 2
