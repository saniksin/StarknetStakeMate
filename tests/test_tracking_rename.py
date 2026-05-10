"""Unit tests for the in-place tag rename flow.

Covers :func:`services.tracking_service.rename_tracking_entry` — the new
service helper used by both the bot (no entry-point yet, kept for symmetry
with the add-flow) and the Mini App PATCH endpoint. Pure-function: no
network, no DB. The DB-layer atomic helper has its own tests in
``test_database_atomic.py`` (or inline below — see fixtures).
"""
from __future__ import annotations

import pytest

from services.tracking_service import (
    RenameTrackingError,
    rename_tracking_entry,
)


VALID_ADDR_A = "0x" + "a" * 63
VALID_ADDR_B = "0x" + "b" * 63
VALID_DELEGATOR = "0x" + "d" * 63
VALID_STAKER = "0x" + "e" * 63


# ---------------------------------------------------------------------------
# Validation: label length / whitespace / type
# ---------------------------------------------------------------------------


def test_rename_rejects_empty_label() -> None:
    doc = {"validators": [{"address": VALID_ADDR_A, "label": "old"}]}
    with pytest.raises(RenameTrackingError) as exc:
        rename_tracking_entry(
            doc, kind="validator", address=VALID_ADDR_A, label=""
        )
    assert exc.value.code == "label_empty"


def test_rename_rejects_whitespace_only_label() -> None:
    doc = {"validators": [{"address": VALID_ADDR_A, "label": "old"}]}
    with pytest.raises(RenameTrackingError) as exc:
        rename_tracking_entry(
            doc, kind="validator", address=VALID_ADDR_A, label="   \t \n  "
        )
    assert exc.value.code == "label_empty"


def test_rename_rejects_overlong_label() -> None:
    doc = {"validators": [{"address": VALID_ADDR_A, "label": "old"}]}
    with pytest.raises(RenameTrackingError) as exc:
        rename_tracking_entry(
            doc,
            kind="validator",
            address=VALID_ADDR_A,
            # 65 chars — one over the 64-char ceiling enforced server-side.
            label="x" * 65,
        )
    assert exc.value.code == "label_too_long"


def test_rename_accepts_max_length_64() -> None:
    doc = {"validators": [{"address": VALID_ADDR_A, "label": "old"}]}
    new = "x" * 64
    out, entry = rename_tracking_entry(
        doc, kind="validator", address=VALID_ADDR_A, label=new
    )
    assert entry["label"] == new
    assert out["validators"][0]["label"] == new


def test_rename_strips_surrounding_whitespace() -> None:
    doc = {"validators": [{"address": VALID_ADDR_A, "label": "old"}]}
    out, entry = rename_tracking_entry(
        doc, kind="validator", address=VALID_ADDR_A, label="  Karnot  "
    )
    assert entry["label"] == "Karnot"
    assert out["validators"][0]["label"] == "Karnot"


# ---------------------------------------------------------------------------
# Lookup: address normalization, kind routing, missing entries
# ---------------------------------------------------------------------------


def test_rename_validator_case_insensitive_address() -> None:
    doc = {
        "validators": [{"address": VALID_ADDR_A.lower(), "label": "old"}],
        "delegations": [],
    }
    # Caller passes the same address in upper-case.
    upper = "0x" + "A" * 63
    out, entry = rename_tracking_entry(
        doc, kind="validator", address=upper, label="New"
    )
    assert entry["label"] == "New"
    assert out["validators"][0]["label"] == "New"


def test_rename_validator_not_found() -> None:
    doc = {"validators": [{"address": VALID_ADDR_A, "label": "old"}]}
    with pytest.raises(RenameTrackingError) as exc:
        rename_tracking_entry(
            doc, kind="validator", address=VALID_ADDR_B, label="New"
        )
    assert exc.value.code == "not_found"


def test_rename_delegator_by_delegator_address() -> None:
    """The Mini App identifies a delegation by the *delegator* address
    (matches the URL pattern ``/tracking/delegator/{address}`` where
    ``address`` is what the user can copy from the detail card)."""
    doc = {
        "validators": [],
        "delegations": [
            {
                "delegator": VALID_DELEGATOR,
                "staker": VALID_STAKER,
                "label": "old",
            }
        ],
    }
    out, entry = rename_tracking_entry(
        doc, kind="delegator", address=VALID_DELEGATOR, label="My stake"
    )
    assert entry["label"] == "My stake"
    assert out["delegations"][0]["label"] == "My stake"


def test_rename_delegator_not_found() -> None:
    doc = {
        "validators": [],
        "delegations": [
            {
                "delegator": VALID_DELEGATOR,
                "staker": VALID_STAKER,
                "label": "old",
            }
        ],
    }
    other = "0x" + "f" * 63
    with pytest.raises(RenameTrackingError) as exc:
        rename_tracking_entry(
            doc, kind="delegator", address=other, label="New"
        )
    assert exc.value.code == "not_found"


def test_rename_unknown_kind() -> None:
    doc = {"validators": [], "delegations": []}
    with pytest.raises(RenameTrackingError) as exc:
        rename_tracking_entry(
            doc, kind="bogus", address=VALID_ADDR_A, label="New"
        )
    assert exc.value.code == "invalid_kind"


# ---------------------------------------------------------------------------
# Idempotence: doc untouched when label is identical (still legal — caller
# may be replaying after a network blip).
# ---------------------------------------------------------------------------


def test_rename_to_same_label_succeeds() -> None:
    doc = {"validators": [{"address": VALID_ADDR_A, "label": "Karnot"}]}
    out, entry = rename_tracking_entry(
        doc, kind="validator", address=VALID_ADDR_A, label="Karnot"
    )
    assert entry["label"] == "Karnot"
    assert out["validators"][0]["label"] == "Karnot"


# ---------------------------------------------------------------------------
# Doc shape: other entries unmodified, display_order preserved.
# ---------------------------------------------------------------------------


def test_rename_preserves_other_entries() -> None:
    doc = {
        "validators": [
            {"address": VALID_ADDR_A, "label": "old-A"},
            {"address": VALID_ADDR_B, "label": "old-B"},
        ],
        "delegations": [
            {
                "delegator": VALID_DELEGATOR,
                "staker": VALID_STAKER,
                "label": "old-D",
            }
        ],
    }
    out, _ = rename_tracking_entry(
        doc, kind="validator", address=VALID_ADDR_A, label="new-A"
    )
    # Targeted entry updated.
    assert out["validators"][0]["label"] == "new-A"
    # Other entries untouched.
    assert out["validators"][1]["label"] == "old-B"
    assert out["delegations"][0]["label"] == "old-D"


def test_rename_preserves_display_order() -> None:
    from services.tracking_service import (
        compose_validator_key,
        compose_delegation_key,
    )

    doc = {
        "validators": [{"address": VALID_ADDR_A, "label": "old"}],
        "delegations": [
            {
                "delegator": VALID_DELEGATOR,
                "staker": VALID_STAKER,
                "label": "old-D",
            }
        ],
        "display_order": [
            compose_delegation_key(VALID_DELEGATOR, VALID_STAKER),
            compose_validator_key(VALID_ADDR_A),
        ],
    }
    out, _ = rename_tracking_entry(
        doc, kind="validator", address=VALID_ADDR_A, label="new"
    )
    assert out.get("display_order") == [
        compose_delegation_key(VALID_DELEGATOR, VALID_STAKER),
        compose_validator_key(VALID_ADDR_A),
    ]
