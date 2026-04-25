"""Unit tests for the per-token threshold input parser.

Covers Bug #1 (parser accepts both ``SYMBOL AMOUNT`` and ``AMOUNT SYMBOL``
plus no-space and comma-decimal variants) and the typed error codes that
power Bug #2-aware error messages.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from services.price_service import (
    ThresholdParseError,
    ThresholdParseErrorCode,
    parse_token_threshold,
    reward_symbols,
)


# Real allow-list from the service. The parser must lean on this so that
# the production prompt and the parser cannot drift apart.
ALLOWED = reward_symbols()


# ---------------------------------------------------------------------------
# Bug #2 — semantic guarantee about the allow-list
# ---------------------------------------------------------------------------

def test_reward_symbols_only_strk() -> None:
    """Per-token reward thresholds make sense ONLY for STRK because rewards
    are always paid in STRK regardless of which pool you're in. This test
    pins that invariant; if a future protocol change adds a non-STRK
    reward token, update _REWARD_SYMBOLS deliberately."""
    assert tuple(reward_symbols()) == ("STRK",)


# ---------------------------------------------------------------------------
# Bug #1 — happy paths (every accepted input variant)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("STRK 100", ("STRK", Decimal("100"))),
        ("100 STRK", ("STRK", Decimal("100"))),
        ("strk 100", ("STRK", Decimal("100"))),       # lowercase symbol
        ("100 strk", ("STRK", Decimal("100"))),
        ("StRk 100", ("STRK", Decimal("100"))),       # mixed case
        ("100strk", ("STRK", Decimal("100"))),        # no whitespace, num→sym
        ("strk100", ("STRK", Decimal("100"))),        # no whitespace, sym→num
        ("  STRK   100  ", ("STRK", Decimal("100"))), # extra whitespace
        ("STRK 100.5", ("STRK", Decimal("100.5"))),   # dot decimal
        ("STRK 100,5", ("STRK", Decimal("100.5"))),   # comma decimal
        ("100,5 STRK", ("STRK", Decimal("100.5"))),
        ("STRK 0", ("STRK", Decimal("0"))),           # zero = clear threshold
        ("0 STRK", ("STRK", Decimal("0"))),
        ("STRK 0.001", ("STRK", Decimal("0.001"))),   # small value
    ],
)
def test_parse_accepts_all_supported_variants(
    text: str, expected: tuple[str, Decimal]
) -> None:
    assert parse_token_threshold(text, ALLOWED) == expected


# ---------------------------------------------------------------------------
# Bug #1 — error paths (each error has a specific code so the handler
# can render a precise localized message instead of one generic blob)
# ---------------------------------------------------------------------------

def test_parse_empty_string() -> None:
    with pytest.raises(ThresholdParseError) as exc:
        parse_token_threshold("", ALLOWED)
    assert exc.value.code == ThresholdParseErrorCode.EMPTY


def test_parse_whitespace_only() -> None:
    with pytest.raises(ThresholdParseError) as exc:
        parse_token_threshold("   ", ALLOWED)
    assert exc.value.code == ThresholdParseErrorCode.EMPTY


def test_parse_only_number_missing_symbol() -> None:
    """User typed ``100`` — must say 'укажи символ', NOT 'введите число'."""
    with pytest.raises(ThresholdParseError) as exc:
        parse_token_threshold("100", ALLOWED)
    assert exc.value.code == ThresholdParseErrorCode.MISSING_SYMBOL


def test_parse_only_decimal_missing_symbol() -> None:
    with pytest.raises(ThresholdParseError) as exc:
        parse_token_threshold("100.5", ALLOWED)
    assert exc.value.code == ThresholdParseErrorCode.MISSING_SYMBOL


def test_parse_only_symbol_missing_amount() -> None:
    with pytest.raises(ThresholdParseError) as exc:
        parse_token_threshold("STRK", ALLOWED)
    assert exc.value.code == ThresholdParseErrorCode.MISSING_AMOUNT


def test_parse_unknown_symbol_carries_typed_token_in_detail() -> None:
    """Unknown symbol error must include the typed symbol so the message
    can echo it back to the user, e.g. ``Unknown symbol: WBTC``."""
    with pytest.raises(ThresholdParseError) as exc:
        parse_token_threshold("WBTC 0.001", ALLOWED)
    assert exc.value.code == ThresholdParseErrorCode.UNKNOWN_SYMBOL
    assert exc.value.detail == "WBTC"


def test_parse_unknown_symbol_in_lowercase_still_unknown() -> None:
    with pytest.raises(ThresholdParseError) as exc:
        parse_token_threshold("0.001 wbtc", ALLOWED)
    assert exc.value.code == ThresholdParseErrorCode.UNKNOWN_SYMBOL


def test_parse_negative_amount_rejected() -> None:
    with pytest.raises(ThresholdParseError) as exc:
        parse_token_threshold("STRK -10", ALLOWED)
    assert exc.value.code == ThresholdParseErrorCode.NEGATIVE


def test_parse_too_many_values() -> None:
    with pytest.raises(ThresholdParseError) as exc:
        parse_token_threshold("STRK 100 200", ALLOWED)
    assert exc.value.code == ThresholdParseErrorCode.TOO_MANY_TOKENS


def test_parse_two_symbols_rejected() -> None:
    with pytest.raises(ThresholdParseError) as exc:
        parse_token_threshold("STRK WBTC 100", ALLOWED)
    assert exc.value.code == ThresholdParseErrorCode.TOO_MANY_TOKENS


# ---------------------------------------------------------------------------
# Generic-ness: parser must respect whatever allow-list it gets, so the
# same code can be reused if the protocol later adds another reward token.
# ---------------------------------------------------------------------------

def test_parse_with_custom_allowlist_preserves_canonical_casing() -> None:
    """Using a hypothetical multi-symbol allow-list, the parser must return
    the canonical (original-cased) symbol — e.g. ``tBTC`` not ``TBTC`` —
    so downstream storage stays normalized."""
    custom = ("STRK", "tBTC", "SolvBTC")
    assert parse_token_threshold("tbtc 0.5", custom) == (
        "tBTC", Decimal("0.5")
    )
    assert parse_token_threshold("0.5 SOLVBTC", custom) == (
        "SolvBTC", Decimal("0.5")
    )
