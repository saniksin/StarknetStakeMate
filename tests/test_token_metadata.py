"""Decoding of ``symbol()`` across the two shapes Starknet tokens use.

This path was dead code on mainnet for a long time — every staking token
was in the hard-coded ``_WELL_KNOWN`` table, so nothing exercised the
on-chain lookup, and it had silently stopped working under starknet-py
0.30 (the hand-written ABI produced a Contract with zero functions).
Sepolia's test wrappers are not in that table, which is how it surfaced.
"""
from __future__ import annotations

import pytest

from services.token_service import _decode_symbol, _felt_to_ascii


def _byte_array(text: str) -> list[int]:
    """Encode a short string the way Cairo serializes a ``ByteArray``."""
    raw = text.encode("utf-8")
    full, pending = divmod(len(raw), 31)
    words = [int.from_bytes(raw[i * 31 : (i + 1) * 31], "big") for i in range(full)]
    pending_word = int.from_bytes(raw[full * 31 :], "big") if pending else 0
    return [full, *words, pending_word, pending]


def test_decodes_felt252_short_string() -> None:
    # 0x5354524b == "STRK" — what the older tokens return.
    assert _decode_symbol([0x5354524B]) == "STRK"


def test_decodes_byte_array_short_symbol() -> None:
    # Modern OpenZeppelin-components tokens (EKUBO on mainnet, the Sepolia
    # staking wrappers) answer with a ByteArray instead.
    assert _decode_symbol(_byte_array("EKUBO")) == "EKUBO"


def test_decodes_byte_array_spanning_full_words() -> None:
    long_name = "A" * 35  # one full 31-byte word + a 4-byte remainder
    assert _decode_symbol(_byte_array(long_name)) == long_name


def test_decodes_byte_array_with_no_pending_word() -> None:
    exact = "B" * 31
    encoded = _byte_array(exact)
    assert encoded[0] == 1 and encoded[-1] == 0
    assert _decode_symbol(encoded) == exact


@pytest.mark.parametrize("result", [[], [0], [1]])
def test_empty_or_zero_symbol_is_none(result) -> None:
    # ``[1]`` is a ByteArray header promising a word that isn't there.
    assert _decode_symbol(result) is None


def test_malformed_byte_array_is_none() -> None:
    # Truncated: claims two full words, supplies neither.
    assert _decode_symbol([2, 0x41]) is None


def test_non_utf8_payload_is_none() -> None:
    assert _decode_symbol([0, 0xFFFE, 2]) is None


def test_felt_to_ascii_still_handles_trailing_space() -> None:
    assert _felt_to_ascii(int.from_bytes(b"STRK ", "big")) == "STRK"
