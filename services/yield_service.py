"""Yield calculator: per-validator/delegator breakdown for the Mini App's
``Yield`` tab.

The endpoint ``GET /api/v1/users/me/yield-data`` returns the raw stake data
the frontend needs to compute yield numbers for any user-supplied APR. The
backend deliberately does NOT bake APR into its responses — the user types
APR in the UI and the math runs client-side. This keeps the cache key
small (no APR) and prevents server-side staleness from coupling to a
user's settings.

What the service actually does:

  - Read the user's tracking_data via ``fetch_tracking_entries`` (already
    parallelises every staker_pool_info / pool_member_info_v1 call).
  - For each tracked validator: emit one ``YieldPoolBreakdown`` per pool
    where own>0 OR delegated>0.
  - For each tracked delegator: emit one ``YieldPoolBreakdown`` per pool
    they're a member of (positions list is already filtered to non-empty).
  - Attach the latest USD price snapshot per token symbol; missing prices
    serialize as ``null``.
  - Cache the assembled payload per ``user_id`` for 60 seconds. Validators
    are added rarely; the heavy lifting (5+ RPC reads) doesn't need to run
    every time the user re-opens the tab.

Pure math helpers (``compute_validator_pool_yield`` /
``compute_delegator_pool_yield``) are exposed for the test suite and as a
canonical reference for the frontend implementation; the API itself
returns raw amounts and lets the UI multiply by APR.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Optional

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from services.price_service import get_usd_prices
from services.staking_dto import DelegatorMultiPositions, ValidatorInfo
from services.tracking_service import TrackingEntry, fetch_tracking_entries


# ---------------------------------------------------------------------------
# Pure math helpers (exposed for tests and as a canonical reference)
# ---------------------------------------------------------------------------


# Months and days the linear yield model uses. We deliberately do NOT
# compound; users want a "right-now what would I earn" intuition and a
# 1-year compound math conversation derails that.
_DAYS_PER_YEAR = Decimal(365)
_MONTHS_PER_YEAR = Decimal(12)


def pool_apr_for_symbol(
    symbol: Optional[str],
    *,
    strk_apr: Decimal,
    btc_apr: Decimal,
) -> Decimal:
    """Map a pool token symbol to the right APR input.

    STRK pools use the STRK APR. WBTC / LBTC / tBTC / SolvBTC / strkBTC
    all share the BTC APR (every BTC wrapper tracks the same staking
    program). Unknown symbols fall back to STRK APR — that keeps the
    math from silently multiplying by zero if a future token comes
    online before the table here is updated.
    """
    if not symbol:
        return strk_apr
    s = symbol.upper()
    if s == "STRK":
        return strk_apr
    if s in {"WBTC", "LBTC", "TBTC", "SOLVBTC", "STRKBTC"}:
        return btc_apr
    return strk_apr


def compute_validator_pool_yield(
    *,
    own: Decimal,
    delegated: Decimal,
    apr_pct: Decimal,
    commission_bps: int,
) -> Decimal:
    """Annual token yield for a validator's position in one pool.

    Formula::

        own_yield        = own * (apr / 100)
        commission_yield = delegated * (apr / 100) * (commission_bps / 10000)
        total            = own_yield + commission_yield

    Linear, no compounding. Returns Decimal in the pool's token (caller
    converts to USD).
    """
    apr_frac = apr_pct / Decimal(100)
    own_yield = own * apr_frac
    commission_frac = Decimal(commission_bps) / Decimal(10000)
    commission_yield = delegated * apr_frac * commission_frac
    return own_yield + commission_yield


def compute_delegator_pool_yield(
    *,
    delegated: Decimal,
    apr_pct: Decimal,
    commission_bps: int,
) -> Decimal:
    """Annual token yield for a delegator's position in one pool.

    Formula::

        net_apr = (apr / 100) * ((10000 - commission_bps) / 10000)
        total   = delegated * net_apr

    Linear, no compounding. Returns Decimal in the pool's token.
    """
    apr_frac = apr_pct / Decimal(100)
    keep_frac = (Decimal(10000) - Decimal(commission_bps)) / Decimal(10000)
    return delegated * apr_frac * keep_frac


def monthly_from_year(year: Decimal) -> Decimal:
    """Linear: monthly = year / 12."""
    return year / _MONTHS_PER_YEAR


def daily_from_year(year: Decimal) -> Decimal:
    """Linear: daily = year / 365."""
    return year / _DAYS_PER_YEAR


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


class YieldPoolBreakdown(BaseModel):
    """One pool's stake breakdown for the yield calculator.

    Amounts are encoded as **strings** (base units, no decimals applied).
    JSON numbers can't represent the full STRK-18-decimal range without
    precision loss past ~10^15, and a portfolio of 100k STRK is already
    1e23 base units. The frontend parses to ``BigInt`` and converts to
    Number only after dividing by ``10**decimals``.

    ``price_usd`` is the latest CoinGecko snapshot for ``symbol``, or
    ``None`` when the price service has no quote (the frontend renders
    "—" in the USD column for those pools).
    """

    model_config = ConfigDict(populate_by_name=True)

    symbol: str = Field(description="Token symbol, e.g. 'STRK', 'WBTC'.")
    decimals: int = Field(description="Token decimals (18 for STRK, 8 for WBTC).")
    own: str = Field(
        default="0",
        description=(
            "Validator's own stake in this pool, base units as a decimal "
            "string. Always '0' for delegator-side pool entries."
        ),
    )
    delegated: str = Field(
        default="0",
        description=(
            "Total delegated amount in this pool from the user's "
            "perspective. For validator entries: aggregate delegated "
            "stake across the pool. For delegator entries: this user's "
            "amount in the pool."
        ),
    )
    price_usd: Optional[Decimal] = Field(
        default=None,
        description="Latest USD price for ``symbol``, or ``null`` if unknown.",
    )


class YieldValidatorEntry(BaseModel):
    """One tracked validator with its multi-pool breakdown."""

    address: str
    label: str = ""
    commission_bps: int = Field(
        default=0,
        description=(
            "Validator commission in basis points (1500 = 15%). 0 when "
            "the on-chain commission is unknown — a warning is logged."
        ),
    )
    pools: list[YieldPoolBreakdown] = Field(default_factory=list)


class YieldDelegatorEntry(BaseModel):
    """One tracked delegator with its multi-pool breakdown.

    Carries the staker's address+label so the frontend can show "alice
    via anastasiia" without an extra round-trip.
    """

    address: str
    label: str = ""
    validator_address: str = ""
    validator_label: str = ""
    commission_bps: int = 0
    pools: list[YieldPoolBreakdown] = Field(default_factory=list)


class YieldPayload(BaseModel):
    """Top-level yield-data response."""

    validators: list[YieldValidatorEntry] = Field(default_factory=list)
    delegators: list[YieldDelegatorEntry] = Field(default_factory=list)
    stale: bool = Field(
        default=False,
        description=(
            "True when an upstream RPC failed and we fell back to a "
            "previously cached payload. Frontend can render a small "
            "warning chip when set."
        ),
    )
    fetched_at: str = Field(
        default="",
        description="ISO-8601 UTC timestamp when this payload was assembled.",
    )


# ---------------------------------------------------------------------------
# Cache (per-user, 60s TTL)
# ---------------------------------------------------------------------------


# Reasonable default — validators are added rarely (10/user cap), the
# CoinGecko cache TTL is 5min, and the user re-opens the tab a few times
# per session at most. 60s leaves room for a fresh build per minute
# without burning RPC budget.
_CACHE_TTL_SECONDS = 60

# Module-level dict keyed by user_id. Each entry is ``(payload, fetched_at_unix)``.
# Async-safe via the per-user lock dict below.
_CACHE: dict[int, tuple[YieldPayload, float]] = {}
_CACHE_LOCKS: dict[int, asyncio.Lock] = {}


def invalidate_cache(*, user_id: Optional[int] = None) -> None:
    """Drop the cached payload for one user (or all of them).

    Used by tests and by mutation paths in the future (e.g. removing a
    tracked validator should reset the user's cached yield-data so the
    next read sees the new shape).
    """
    if user_id is None:
        _CACHE.clear()
        _CACHE_LOCKS.clear()
        return
    _CACHE.pop(user_id, None)
    _CACHE_LOCKS.pop(user_id, None)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def _validator_pool_breakdowns(
    info: ValidatorInfo,
    prices: dict[str, Decimal],
) -> list[YieldPoolBreakdown]:
    """One ``YieldPoolBreakdown`` per non-empty pool the validator runs.

    Validator's *own* stake (``amount_own_raw``) is denominated in STRK
    and lives in the STRK pool. We attribute it to the matching pool by
    symbol; any non-STRK pool gets ``own="0"``.
    """
    out: list[YieldPoolBreakdown] = []
    own_strk_raw = int(info.amount_own_raw or 0)
    for p in info.pools:
        sym = p.token_symbol or "UNKNOWN"
        delegated_raw = int(p.amount_raw or 0)
        own_for_pool = own_strk_raw if (sym.upper() == "STRK") else 0
        if delegated_raw == 0 and own_for_pool == 0:
            # Filter empty pools — keeps the response compact when a
            # validator has opted into multi-token but only fills STRK.
            continue
        price = prices.get(sym) or prices.get(sym.upper())
        out.append(
            YieldPoolBreakdown(
                symbol=sym,
                decimals=_decimals_from_pool(p),
                own=str(own_for_pool),
                delegated=str(delegated_raw),
                price_usd=price,
            )
        )
    return out


def _decimals_from_pool(p) -> int:
    """Recover the token decimals from ``raw / decimal`` ratio.

    The ``PoolInfoDto`` dropped explicit ``decimals`` in favour of pre-
    computed ``amount_decimal``. We reverse-derive the scale here so the
    Mini App can render token amounts without round-tripping through the
    token registry. ``amount_decimal == 0`` (empty pool) falls back to
    18 — empty pools are filtered upstream so this only matters when a
    future caller passes one through.
    """
    if p.amount_raw == 0 or p.amount_decimal == 0:
        # Map by symbol fallback — covers freshly-created pools whose
        # amounts haven't propagated yet.
        sym = (p.token_symbol or "").upper()
        if sym in {"WBTC", "SOLVBTC", "STRKBTC"}:
            return 8
        return 18
    # raw / 10^decimals == amount_decimal  ⇒  decimals = log10(raw/amount_decimal)
    ratio = Decimal(p.amount_raw) / p.amount_decimal
    # Find the integer power of 10 closest to the ratio.
    # In practice the ratio is exactly 10^N because amount_decimal is
    # constructed from raw via raw_to_decimal — but defend against future
    # rounding by anchoring to the known token-decimal vocabulary.
    for cand in (8, 18):
        if abs(ratio - (Decimal(10) ** cand)) / (Decimal(10) ** cand) < Decimal("0.001"):
            return cand
    # Fall through to 18.
    return 18


def _delegator_pool_breakdowns(
    multi: DelegatorMultiPositions,
    prices: dict[str, Decimal],
) -> list[YieldPoolBreakdown]:
    out: list[YieldPoolBreakdown] = []
    for pos in multi.positions:
        sym = pos.token_symbol or "STRK"
        delegated_raw = int(pos.amount_raw or 0)
        if delegated_raw == 0:
            continue
        price = prices.get(sym) or prices.get(sym.upper())
        # Decimals: reverse from raw/decimal where possible, else fall
        # back by symbol vocabulary (matches token_service _WELL_KNOWN).
        if pos.amount_decimal and pos.amount_raw:
            ratio = Decimal(pos.amount_raw) / pos.amount_decimal
            decimals = 18
            for cand in (8, 18):
                if abs(ratio - (Decimal(10) ** cand)) / (Decimal(10) ** cand) < Decimal("0.001"):
                    decimals = cand
                    break
        else:
            decimals = 8 if sym.upper() in {"WBTC", "SOLVBTC", "STRKBTC"} else 18
        out.append(
            YieldPoolBreakdown(
                symbol=sym,
                decimals=decimals,
                own="0",
                delegated=str(delegated_raw),
                price_usd=price,
            )
        )
    return out


def _label_for_validator_lookup(validators: list[YieldValidatorEntry], staker_address: str) -> str:
    """Return the user's label for ``staker_address`` if they also track
    that validator directly; '' otherwise."""
    needle = (staker_address or "").lower()
    for v in validators:
        if (v.address or "").lower() == needle:
            return v.label
    return ""


async def _build_payload_uncached(tracking_data: Optional[str]) -> YieldPayload:
    """Assemble the yield payload from scratch — no cache lookup."""
    entries: list[TrackingEntry] = await fetch_tracking_entries(tracking_data)
    try:
        prices = await get_usd_prices()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"yield_service: price fetch failed: {exc}")
        prices = {}

    validators: list[YieldValidatorEntry] = []
    delegators: list[YieldDelegatorEntry] = []

    for e in entries:
        if e.kind == "validator" and isinstance(e.data, ValidatorInfo):
            commission_bps = e.data.commission_bps
            if commission_bps is None:
                logger.warning(
                    f"yield_service: missing commission_bps for {e.address}; defaulting to 0"
                )
                commission_bps = 0
            pools = _validator_pool_breakdowns(e.data, prices)
            validators.append(
                YieldValidatorEntry(
                    address=e.address,
                    label=e.label or "",
                    commission_bps=int(commission_bps),
                    pools=pools,
                )
            )
        elif e.kind == "delegator" and isinstance(e.data, DelegatorMultiPositions):
            # Commission is per-position; in practice every pool of the same
            # staker shares one commission. Pick the first non-zero, fall
            # back to 0.
            commission_bps = 0
            for pos in e.data.positions:
                if pos.commission_bps:
                    commission_bps = int(pos.commission_bps)
                    break
            pools = _delegator_pool_breakdowns(e.data, prices)
            delegators.append(
                YieldDelegatorEntry(
                    address=e.address,
                    label=e.label or "",
                    validator_address=e.pool or "",
                    validator_label=_label_for_validator_lookup(validators, e.pool or ""),
                    commission_bps=commission_bps,
                    pools=pools,
                )
            )

    return YieldPayload(
        validators=validators,
        delegators=delegators,
        stale=False,
        fetched_at=_utc_now_iso(),
    )


async def build_yield_payload(
    *, user_id: int, tracking_data: Optional[str]
) -> YieldPayload:
    """Cached wrapper around :func:`_build_payload_uncached`.

    Per-user cache with a 60s TTL. Concurrent requests for the same
    user behind a fresh cache miss share a single fetch via
    :class:`asyncio.Lock` (otherwise opening the Yield tab twice in the
    same second would fire two parallel RPC fan-outs).
    """
    now = time.time()
    cached = _CACHE.get(user_id)
    if cached is not None and (now - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]

    lock = _CACHE_LOCKS.setdefault(user_id, asyncio.Lock())
    async with lock:
        # Double-check inside the lock — another coroutine may have
        # populated the cache while we were waiting.
        cached = _CACHE.get(user_id)
        now = time.time()
        if cached is not None and (now - cached[1]) < _CACHE_TTL_SECONDS:
            return cached[0]

        try:
            payload = await _build_payload_uncached(tracking_data)
        except Exception as exc:  # noqa: BLE001
            # If we have a stale cache, mark it stale and serve it. If
            # not, re-raise so the endpoint surfaces a 500 — that's
            # actionable; silent empty payloads are not.
            logger.error(f"yield_service: build failed for user_id={user_id}: {exc}")
            if cached is not None:
                stale_payload = cached[0].model_copy(update={"stale": True})
                return stale_payload
            raise

        _CACHE[user_id] = (payload, now)
        return payload
