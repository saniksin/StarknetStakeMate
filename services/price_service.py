"""Token-to-USD price lookups via CoinGecko.

Public, key-less endpoint. We need only a handful of tokens (STRK + the BTC
wrappers on Starknet), so one request answers the whole batch and gets
cached for 5 minutes — well under CoinGecko's free-tier limits.

The price is a notification-helper, not consensus-critical: a stale or
missing quote should never block alerts. ``get_usd_prices()`` therefore
falls back to the previous successful snapshot on any HTTP failure and
returns an empty dict on the very first failure (so callers can simply
``prices.get(symbol, Decimal(0))`` without branching).
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Iterable

import aiohttp
from loguru import logger


# Map token symbol → CoinGecko ID. Every BTC wrapper tracks BTC closely
# enough that the fluctuation between them is below the user-visible noise
# floor for "did I cross $5", so we deliberately use ``bitcoin`` for all of
# them rather than chasing per-wrapper IDs.
_SYMBOL_TO_CG_ID: dict[str, str] = {
    "STRK": "starknet",
    "WBTC": "bitcoin",
    "LBTC": "bitcoin",
    "tBTC": "bitcoin",
    "SolvBTC": "bitcoin",
}

_TTL = int(os.getenv("PRICE_CACHE_TTL", "300"))  # 5 min default


class PriceCache:
    """In-process snapshot of {symbol: USD price}.

    Two layers of resilience:
      - successful fetch updates ``_snapshot`` and ``_fetched_at``;
      - on fetch failure, ``get_usd_prices`` returns the last good snapshot
        until it ages past ``_TTL * 4``, then we give up and return ``{}``.
    """

    def __init__(self) -> None:
        self._snapshot: dict[str, Decimal] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> dict[str, Decimal]:
        now = time.time()
        if self._snapshot and (now - self._fetched_at) < _TTL:
            return dict(self._snapshot)
        async with self._lock:
            now = time.time()
            if self._snapshot and (now - self._fetched_at) < _TTL:
                return dict(self._snapshot)
            fresh = await _fetch_coingecko()
            if fresh:
                self._snapshot = fresh
                self._fetched_at = now
            elif self._snapshot and (now - self._fetched_at) > _TTL * 4:
                # Stale beyond 4× TTL → drop the cache so callers see "no data"
                # rather than week-old prices.
                logger.warning("price cache exceeded staleness budget; clearing")
                self._snapshot = {}
            return dict(self._snapshot)


async def _fetch_coingecko() -> dict[str, Decimal]:
    ids = ",".join(sorted(set(_SYMBOL_TO_CG_ID.values())))
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids}&vs_currencies=usd"
    )
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"coingecko HTTP {resp.status}: {await resp.text()}")
                    return {}
                payload = await resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"coingecko fetch failed: {exc}")
        return {}

    out: dict[str, Decimal] = {}
    for symbol, cg_id in _SYMBOL_TO_CG_ID.items():
        try:
            price = payload[cg_id]["usd"]
            out[symbol] = Decimal(str(price))
        except (KeyError, TypeError):
            continue
    return out


_cache = PriceCache()


async def get_usd_prices() -> dict[str, Decimal]:
    """Return ``{symbol: USD_price}`` for tokens we know how to price.

    Symbols not in the result simply have no quote; callers must handle
    missing keys gracefully.
    """
    return await _cache.get()


def usd_value(amount: Decimal, symbol: str | None, prices: dict[str, Decimal]) -> Decimal:
    """Best-effort USD valuation of a token amount. Returns 0 when unknown."""
    if amount == 0 or not symbol:
        return Decimal(0)
    price = prices.get(symbol)
    if price is None:
        return Decimal(0)
    return amount * price


def known_symbols() -> Iterable[str]:
    """Symbols the price service can quote (USD valuation only).

    Used by the price layer / portfolio summaries to decide which tokens
    we can attach a USD figure to. NOT used to gate notification thresholds
    — for that see :func:`reward_symbols`.
    """
    return _SYMBOL_TO_CG_ID.keys()


# Tokens in which Starknet staking rewards are actually paid out.
#
# Per the V2 protocol: a delegator's *stake* can be in any active token
# (STRK or a BTC wrapper), but *rewards* are always paid in STRK
# regardless of pool. Therefore a per-token reward threshold is only
# meaningful for STRK — a "0.001 WBTC" threshold would never trigger
# because no WBTC reward stream exists. This list is the single source
# of truth for both the input prompt and the parser/validator.
_REWARD_SYMBOLS: tuple[str, ...] = ("STRK",)


def reward_symbols() -> tuple[str, ...]:
    """Symbols that are valid for per-token reward-notification thresholds.

    Currently STRK only — see ``_REWARD_SYMBOLS`` for rationale.
    """
    return _REWARD_SYMBOLS


# ---------------------------------------------------------------------------
# Per-token threshold input parser
# ---------------------------------------------------------------------------
#
# Pure, side-effect-free parser used by the Telegram FSM handler. Lives next
# to ``reward_symbols()`` so the symbol list is a single source of truth.
#
# Accepts the user's natural input variants:
#   "STRK 100"         → ("STRK", 100)
#   "100 STRK"         → ("STRK", 100)
#   "100strk"          → ("STRK", 100)        (no space, case-insensitive)
#   "strk100"          → ("STRK", 100)
#   "100,5 STRK"       → ("STRK", 100.5)      (comma decimal separator)
#   "STRK 0"           → ("STRK", 0)          (caller treats 0 as "remove")
#
# Errors are typed via :class:`ThresholdParseError.code` so the handler can
# reply with a precise localized message (missing symbol, missing amount,
# unknown symbol, malformed number, negative number) instead of one generic
# "введите число".


class ThresholdParseErrorCode(Enum):
    EMPTY = "empty"
    MISSING_SYMBOL = "missing_symbol"
    MISSING_AMOUNT = "missing_amount"
    UNKNOWN_SYMBOL = "unknown_symbol"
    BAD_NUMBER = "bad_number"
    NEGATIVE = "negative"
    TOO_MANY_TOKENS = "too_many_tokens"


@dataclass(frozen=True)
class ThresholdParseError(Exception):
    code: ThresholdParseErrorCode
    detail: str = ""        # e.g. the unknown symbol the user typed

    def __str__(self) -> str:  # pragma: no cover — diagnostic only
        return f"{self.code.value}: {self.detail}" if self.detail else self.code.value


# Number: optional sign, digits, optional . or , decimal part. We then
# normalise commas → dots before passing to Decimal.
_NUMBER_RE = re.compile(r"[+-]?\d+(?:[.,]\d+)?|[+-]?[.,]\d+")
# Symbol: at least one ASCII letter. Allow lower/upper mix; we'll
# canonicalise by case-insensitive lookup against the allow-list.
_SYMBOL_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def parse_token_threshold(
    text: str, allowed_symbols: Iterable[str]
) -> tuple[str, Decimal]:
    """Parse a per-token threshold input string.

    Returns ``(canonical_symbol, amount)`` on success.

    Raises :class:`ThresholdParseError` with a typed ``code`` on any
    validation failure so the caller can pick a specific UI message.

    Accepts both ``SYMBOL AMOUNT`` and ``AMOUNT SYMBOL`` orderings, with or
    without whitespace, and either ``.`` or ``,`` as decimal separator.
    Symbol matching is case-insensitive against ``allowed_symbols``; the
    returned symbol is the canonical (original-case) entry from that list.
    """
    if text is None:
        raise ThresholdParseError(ThresholdParseErrorCode.EMPTY)
    raw = text.strip()
    if not raw:
        raise ThresholdParseError(ThresholdParseErrorCode.EMPTY)

    # Build a case-insensitive lookup that preserves canonical casing
    # (e.g. "tBTC", "SolvBTC" — even though the current allow-list is
    # STRK-only, the parser stays generic so it can be reused if the
    # protocol ever adds another reward token).
    canonical_by_upper = {s.upper(): s for s in allowed_symbols}

    numbers = _NUMBER_RE.findall(raw)
    # Strip away every number to find the residual symbol portion.
    residual = _NUMBER_RE.sub(" ", raw).strip()
    symbol_tokens = _SYMBOL_RE.findall(residual)

    if len(numbers) > 1:
        raise ThresholdParseError(ThresholdParseErrorCode.TOO_MANY_TOKENS)
    if len(symbol_tokens) > 1:
        raise ThresholdParseError(ThresholdParseErrorCode.TOO_MANY_TOKENS)

    if not numbers and not symbol_tokens:
        raise ThresholdParseError(ThresholdParseErrorCode.EMPTY)
    if not symbol_tokens:
        raise ThresholdParseError(ThresholdParseErrorCode.MISSING_SYMBOL)
    if not numbers:
        raise ThresholdParseError(ThresholdParseErrorCode.MISSING_AMOUNT)

    symbol_in = symbol_tokens[0]
    canonical = canonical_by_upper.get(symbol_in.upper())
    if canonical is None:
        raise ThresholdParseError(
            ThresholdParseErrorCode.UNKNOWN_SYMBOL, detail=symbol_in
        )

    number_str = numbers[0].replace(",", ".")
    try:
        amount = Decimal(number_str)
    except (InvalidOperation, ValueError):
        raise ThresholdParseError(
            ThresholdParseErrorCode.BAD_NUMBER, detail=number_str
        ) from None
    if amount < 0:
        raise ThresholdParseError(ThresholdParseErrorCode.NEGATIVE)

    return canonical, amount
