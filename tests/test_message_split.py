"""Long-message chunking for ``render_user_tracking_chunks``.

Telegram silently truncates messages over 4096 characters in HTML mode,
which on a busy /get_full_info digest (8+ validators with BTC pools)
swallowed the rewards footer. The split helper packs cards into
multiple chunks without breaking any single card.
"""
from __future__ import annotations

from services.tracking_service import _split_into_chunks, _TELEGRAM_MSG_LIMIT


def test_short_input_packs_into_single_chunk() -> None:
    parts = ["a" * 100, "b" * 100, "c" * 100]
    chunks = _split_into_chunks(parts)
    assert len(chunks) == 1
    assert chunks[0] == "a" * 100 + "\n\n" + "b" * 100 + "\n\n" + "c" * 100


def test_long_input_splits_at_card_boundaries() -> None:
    """Each part is ~1500 chars — three of them plus glue overflow the
    limit, so we should see two chunks."""
    parts = ["x" * 1500 for _ in range(3)]
    chunks = _split_into_chunks(parts)
    assert len(chunks) >= 2
    # No chunk exceeds the cap.
    for c in chunks:
        assert len(c) <= _TELEGRAM_MSG_LIMIT
    # Total content preserved (glue ≠ data).
    rejoined = "\n\n".join(chunks)
    expected = "\n\n".join(parts)
    assert rejoined == expected


def test_oversized_single_part_passes_through_alone() -> None:
    """If one card exceeds the cap on its own, we let it through —
    Telegram will clip rather than us truncating mid-HTML and breaking
    the parse."""
    big = "y" * (_TELEGRAM_MSG_LIMIT + 200)
    chunks = _split_into_chunks([big, "tail"])
    assert len(chunks) == 2
    assert chunks[0] == big
    assert chunks[1] == "tail"


def test_empty_input_returns_empty_list() -> None:
    assert _split_into_chunks([]) == []
