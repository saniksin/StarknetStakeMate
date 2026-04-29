"""Unit tests for the tracking-reorder service helpers.

Covers :func:`services.tracking_service.reorder_tracking_doc` directly
since the database wrapper is just a thin atomic shell around it. The
permissive matching rules (case-insensitive, partial input, unknown
keys ignored) need explicit coverage so a future refactor can't quietly
drop entries by tightening validation.
"""
from __future__ import annotations

import copy

import pytest

from services.tracking_service import reorder_tracking_doc


def _doc():
    return {
        "validators": [
            {"address": "0x" + "a" * 63, "label": "alpha"},
            {"address": "0x" + "b" * 63, "label": "beta"},
            {"address": "0x" + "c" * 63, "label": "gamma"},
        ],
        "delegations": [
            {"delegator": "0x" + "1" * 63, "staker": "0x" + "a" * 63, "label": ""},
            {"delegator": "0x" + "2" * 63, "staker": "0x" + "b" * 63, "label": ""},
        ],
    }


def _addrs(doc):
    return [v["address"] for v in doc["validators"]]


def _pairs(doc):
    return [(d["delegator"], d["staker"]) for d in doc["delegations"]]


def test_reorder_validators_happy_path():
    doc = _doc()
    new = reorder_tracking_doc(
        doc,
        validators_order=["0x" + "c" * 63, "0x" + "a" * 63, "0x" + "b" * 63],
        delegations_order=None,
    )
    assert _addrs(new) == ["0x" + "c" * 63, "0x" + "a" * 63, "0x" + "b" * 63]
    # Delegations untouched (we passed ``None`` for that side).
    assert _pairs(new) == _pairs(doc)


def test_reorder_does_not_mutate_input():
    doc = _doc()
    snap = copy.deepcopy(doc)
    reorder_tracking_doc(
        doc,
        validators_order=["0x" + "c" * 63],
        delegations_order=None,
    )
    assert doc == snap, "reorder must not mutate the input doc"


def test_reorder_case_insensitive_match():
    doc = _doc()
    # User pastes upper-case hex — should still match the stored entries.
    new = reorder_tracking_doc(
        doc,
        validators_order=["0x" + "C" * 63, "0x" + "B" * 63, "0x" + "A" * 63],
        delegations_order=None,
    )
    assert _addrs(new) == ["0x" + "c" * 63, "0x" + "b" * 63, "0x" + "a" * 63]


def test_reorder_unknown_key_ignored():
    """Keys that don't match any existing entry are silently dropped.

    This keeps the endpoint idempotent against a concurrent ``DELETE`` —
    the user might try to reorder an entry that's already been removed
    on another tab. We refuse to 422 on that race; the live row simply
    doesn't get touched."""
    doc = _doc()
    new = reorder_tracking_doc(
        doc,
        validators_order=[
            "0x" + "f" * 63,  # no such entry
            "0x" + "c" * 63,
            "0x" + "a" * 63,
            # ``b`` intentionally missing — should be appended at the end.
        ],
        delegations_order=None,
    )
    assert _addrs(new) == [
        "0x" + "c" * 63,
        "0x" + "a" * 63,
        "0x" + "b" * 63,
    ]


def test_reorder_partial_keeps_unmentioned_at_end():
    """Permissive partial reorder: ``[c]`` puts c first, others stay."""
    doc = _doc()
    new = reorder_tracking_doc(
        doc,
        validators_order=["0x" + "c" * 63],
        delegations_order=None,
    )
    assert _addrs(new) == [
        "0x" + "c" * 63,
        "0x" + "a" * 63,
        "0x" + "b" * 63,
    ]


def test_reorder_delegations_pair_match():
    doc = _doc()
    new = reorder_tracking_doc(
        doc,
        validators_order=None,
        delegations_order=[
            ("0x" + "2" * 63, "0x" + "b" * 63),
            ("0x" + "1" * 63, "0x" + "a" * 63),
        ],
    )
    assert _pairs(new) == [
        ("0x" + "2" * 63, "0x" + "b" * 63),
        ("0x" + "1" * 63, "0x" + "a" * 63),
    ]
    # Validators untouched.
    assert _addrs(new) == _addrs(doc)


def test_reorder_delegations_only_one_side_changed():
    """``validators_order=None`` must leave validators untouched even
    when delegations changes — and vice versa. Verifies the sides are
    independent (regression against an early version where I shadowed
    ``out["validators"]`` with an empty list when ``None`` was passed)."""
    doc = _doc()
    new = reorder_tracking_doc(
        doc,
        validators_order=None,
        delegations_order=[("0x" + "2" * 63, "0x" + "b" * 63)],
    )
    assert _addrs(new) == _addrs(doc)
    # Delegations: requested pair first, untouched ones at the end.
    assert _pairs(new)[0] == ("0x" + "2" * 63, "0x" + "b" * 63)
    assert ("0x" + "1" * 63, "0x" + "a" * 63) in _pairs(new)


def test_reorder_empty_payload_is_noop():
    """Both lists ``None`` (or empty) → doc returned essentially unchanged."""
    doc = _doc()
    new = reorder_tracking_doc(doc, validators_order=None, delegations_order=None)
    assert _addrs(new) == _addrs(doc)
    assert _pairs(new) == _pairs(doc)

    new2 = reorder_tracking_doc(doc, validators_order=[], delegations_order=[])
    assert _addrs(new2) == _addrs(doc)
    assert _pairs(new2) == _pairs(doc)


def test_reorder_handles_empty_doc():
    doc = {"validators": [], "delegations": []}
    new = reorder_tracking_doc(
        doc,
        validators_order=["0x" + "a" * 63],
        delegations_order=[("0x" + "1" * 63, "0x" + "a" * 63)],
    )
    assert new == {"validators": [], "delegations": []}


def test_reorder_normalizes_legacy_doc_shape():
    """Legacy docs (pre-v2) lacked the ``delegations`` key entirely.
    The normalizer should fill it in so the reorder path doesn't blow
    up on a KeyError when an old user opens the new Mini App."""
    doc = {"validators": [{"address": "0x" + "a" * 63, "label": ""}]}
    new = reorder_tracking_doc(
        doc, validators_order=["0x" + "a" * 63], delegations_order=None
    )
    assert _addrs(new) == ["0x" + "a" * 63]
    assert new["delegations"] == []


def test_reorder_skips_malformed_pair():
    """``[a, b, c]`` (3-tuple) and ``[a]`` (1-tuple) are both ignored —
    we require exactly the (delegator, staker) pair shape. Saves us from
    a malformed Mini-App build accidentally wiping the delegations
    list."""
    doc = _doc()
    new = reorder_tracking_doc(
        doc,
        validators_order=None,
        delegations_order=[
            ("0x" + "1" * 63,),  # too short
            ("0x" + "2" * 63, "0x" + "b" * 63),  # OK
            (),                                   # empty
        ],
    )
    # The well-formed pair gets reordered; the malformed ones are
    # silently skipped, so the other entry just stays at the end.
    pairs = _pairs(new)
    assert pairs[0] == ("0x" + "2" * 63, "0x" + "b" * 63)
    assert ("0x" + "1" * 63, "0x" + "a" * 63) in pairs
