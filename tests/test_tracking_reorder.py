"""Unit tests for the tracking-reorder service helpers.

Covers :func:`services.tracking_service.reorder_tracking_doc` (the
back-compat shim) plus the cross-group ``reorder_tracking_doc_v2``
introduced for the flat ``display_order`` API.

Contract reminder for the post-cross-group world:
  - Raw arrays (``validators``, ``delegations``) stay in **insertion
    order**. They're the source of truth for membership.
  - ``display_order`` (a flat ``list[str]``) is the source of truth for
    **position**. Rendered order = display_order keys (in given order)
    + any unmentioned entries in natural order at the end.
  - The shim API ``reorder_tracking_doc(validators_order=...,
    delegations_order=...)`` synthesizes a flat ``display_order`` from
    its two-list input and writes that — raw arrays untouched.

Tests assert via the *rendered* order (``_rendered`` helper that mirrors
what ``fetch_tracking_entries`` does) rather than via the raw arrays so
they survive the contract change.
"""
from __future__ import annotations

import copy

import pytest

from services.tracking_service import (
    compose_delegation_key,
    compose_validator_key,
    reorder_tracking_doc,
    reorder_tracking_doc_v2,
)


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
    """Raw validator-array order (membership only — not display order)."""
    return [v["address"] for v in doc["validators"]]


def _pairs(doc):
    """Raw delegation-array order (membership only — not display order)."""
    return [(d["delegator"], d["staker"]) for d in doc["delegations"]]


def _rendered(doc):
    """The order ``fetch_tracking_entries`` would render in.

    Mirrors the renderer's logic without making the test async / hitting
    RPC stubs: ``display_order`` keys first (in given order), then any
    unmentioned entries in natural order. Returns a list of stable keys
    so assertions are easy to read."""
    natural: list[str] = []
    for v in doc.get("validators", []):
        addr = v.get("address") or ""
        if addr:
            natural.append(compose_validator_key(addr))
    for d in doc.get("delegations", []):
        delegator = d.get("delegator") or ""
        staker = d.get("staker") or ""
        if delegator and staker:
            natural.append(compose_delegation_key(delegator, staker))
    raw_order = doc.get("display_order")
    if not isinstance(raw_order, list) or not raw_order:
        return natural
    seen: set[str] = set()
    out: list[str] = []
    for k in raw_order:
        if k in natural and k not in seen:
            out.append(k)
            seen.add(k)
    for k in natural:
        if k not in seen:
            out.append(k)
            seen.add(k)
    return out


def test_reorder_validators_happy_path():
    doc = _doc()
    new = reorder_tracking_doc(
        doc,
        validators_order=["0x" + "c" * 63, "0x" + "a" * 63, "0x" + "b" * 63],
        delegations_order=None,
    )
    # Rendered order: c, a, b (validators), then delegations (natural).
    rendered = _rendered(new)
    assert rendered[:3] == [
        compose_validator_key("0x" + "c" * 63),
        compose_validator_key("0x" + "a" * 63),
        compose_validator_key("0x" + "b" * 63),
    ]
    # Raw arrays stay in insertion order (membership-only contract).
    assert _addrs(new) == _addrs(doc)
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
    rendered = _rendered(new)
    assert rendered[:3] == [
        compose_validator_key("0x" + "c" * 63),
        compose_validator_key("0x" + "b" * 63),
        compose_validator_key("0x" + "a" * 63),
    ]


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
    rendered = _rendered(new)
    assert rendered[:3] == [
        compose_validator_key("0x" + "c" * 63),
        compose_validator_key("0x" + "a" * 63),
        compose_validator_key("0x" + "b" * 63),
    ]
    # Stale "f" key must NOT have made it into display_order.
    assert "validator:0x" + "f" * 63 not in (new.get("display_order") or [])


def test_reorder_partial_keeps_unmentioned_at_end():
    """Permissive partial reorder: ``[c]`` puts c first, others stay."""
    doc = _doc()
    new = reorder_tracking_doc(
        doc,
        validators_order=["0x" + "c" * 63],
        delegations_order=None,
    )
    rendered = _rendered(new)
    # Validator side rendered as: c, a, b — c first (requested),
    # then a and b in their natural insertion order.
    assert rendered[:3] == [
        compose_validator_key("0x" + "c" * 63),
        compose_validator_key("0x" + "a" * 63),
        compose_validator_key("0x" + "b" * 63),
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
    rendered = _rendered(new)
    # Validators come first (validators_order=None → natural side),
    # then delegations in the requested pair order.
    assert rendered[3:] == [
        compose_delegation_key("0x" + "2" * 63, "0x" + "b" * 63),
        compose_delegation_key("0x" + "1" * 63, "0x" + "a" * 63),
    ]
    # Raw arrays preserved.
    assert _addrs(new) == _addrs(doc)
    assert _pairs(new) == _pairs(doc)


def test_reorder_delegations_only_one_side_changed():
    """``validators_order=None`` must leave validators untouched even
    when delegations changes — and vice versa. Verifies the sides are
    independent."""
    doc = _doc()
    new = reorder_tracking_doc(
        doc,
        validators_order=None,
        delegations_order=[("0x" + "2" * 63, "0x" + "b" * 63)],
    )
    rendered = _rendered(new)
    # Validators come in natural insertion order (a, b, c) at the start.
    assert rendered[:3] == [
        compose_validator_key("0x" + "a" * 63),
        compose_validator_key("0x" + "b" * 63),
        compose_validator_key("0x" + "c" * 63),
    ]
    # Then delegations: requested pair first, the unmentioned (1, a) at end.
    assert rendered[3] == compose_delegation_key("0x" + "2" * 63, "0x" + "b" * 63)
    assert compose_delegation_key("0x" + "1" * 63, "0x" + "a" * 63) in rendered[3:]


def test_reorder_empty_payload_is_noop():
    """Both lists ``None`` (or empty) → doc returned essentially unchanged."""
    doc = _doc()
    new = reorder_tracking_doc(doc, validators_order=None, delegations_order=None)
    # Rendered order matches the natural one (no display_order written).
    assert _rendered(new) == _rendered(doc)
    # And the raw arrays stay identical.
    assert _addrs(new) == _addrs(doc)
    assert _pairs(new) == _pairs(doc)

    new2 = reorder_tracking_doc(doc, validators_order=[], delegations_order=[])
    assert _rendered(new2) == _rendered(doc)


def test_reorder_handles_empty_doc():
    doc = {"validators": [], "delegations": []}
    new = reorder_tracking_doc(
        doc,
        validators_order=["0x" + "a" * 63],
        delegations_order=[("0x" + "1" * 63, "0x" + "a" * 63)],
    )
    # No entries to reorder — display_order is dropped (empty after pruning).
    assert _addrs(new) == []
    assert _pairs(new) == []
    assert "display_order" not in new


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
    assert _rendered(new) == [compose_validator_key("0x" + "a" * 63)]


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
    rendered = _rendered(new)
    # The well-formed pair gets the leading delegation slot; the other
    # delegation falls into the unmentioned-at-end bucket.
    assert rendered[3] == compose_delegation_key("0x" + "2" * 63, "0x" + "b" * 63)
    assert compose_delegation_key("0x" + "1" * 63, "0x" + "a" * 63) in rendered


# ---------------------------------------------------------------------------
# Cross-group reorder via the new flat API (reorder_tracking_doc_v2)
# ---------------------------------------------------------------------------


def test_v2_cross_group_validator_in_middle_of_delegations():
    """The headline feature: a delegation can sit between two validators
    (and vice versa). Pre-cross-group this was impossible because the
    two arrays were independent. Now ``display_order`` is one flat list."""
    doc = _doc()
    new = reorder_tracking_doc_v2(
        doc,
        order=[
            compose_validator_key("0x" + "a" * 63),
            compose_delegation_key("0x" + "2" * 63, "0x" + "b" * 63),
            compose_validator_key("0x" + "b" * 63),
            compose_delegation_key("0x" + "1" * 63, "0x" + "a" * 63),
            compose_validator_key("0x" + "c" * 63),
        ],
    )
    rendered = _rendered(new)
    assert rendered == [
        compose_validator_key("0x" + "a" * 63),
        compose_delegation_key("0x" + "2" * 63, "0x" + "b" * 63),
        compose_validator_key("0x" + "b" * 63),
        compose_delegation_key("0x" + "1" * 63, "0x" + "a" * 63),
        compose_validator_key("0x" + "c" * 63),
    ]
    # Raw arrays untouched (insertion order preserved).
    assert _addrs(new) == _addrs(doc)
    assert _pairs(new) == _pairs(doc)


def test_v2_concurrent_add_appended_at_end():
    """A new entry shows up in the doc AFTER the user opened reorder
    mode (another tab added it). Their saved ``order`` doesn't mention
    the new key. The renderer must still surface the new entry — at
    the end of the rendered list — instead of hiding it."""
    doc = _doc()
    new = reorder_tracking_doc_v2(
        doc,
        order=[
            # Only mention validators a, b — c was added by another tab
            # mid-drag and isn't in the user's saved order.
            compose_validator_key("0x" + "a" * 63),
            compose_validator_key("0x" + "b" * 63),
        ],
    )
    rendered = _rendered(new)
    # Mentioned ones first, in given order.
    assert rendered[0] == compose_validator_key("0x" + "a" * 63)
    assert rendered[1] == compose_validator_key("0x" + "b" * 63)
    # Unmentioned validator c surfaces somewhere after the mentioned
    # block (natural-order fallback).
    assert compose_validator_key("0x" + "c" * 63) in rendered
    # Same for the unmentioned delegations.
    for k in (
        compose_delegation_key("0x" + "1" * 63, "0x" + "a" * 63),
        compose_delegation_key("0x" + "2" * 63, "0x" + "b" * 63),
    ):
        assert k in rendered


def test_v2_concurrent_delete_unknown_key_ignored():
    """A key in ``order`` references an entry that was deleted in
    another tab. The save must succeed (no 422) and the dead key must
    NOT appear in ``display_order`` — otherwise it'd be carried
    forward forever."""
    doc = _doc()
    ghost = compose_validator_key("0x" + "9" * 63)  # not in doc
    new = reorder_tracking_doc_v2(
        doc,
        order=[
            ghost,
            compose_validator_key("0x" + "a" * 63),
        ],
    )
    persisted = new.get("display_order") or []
    assert ghost not in persisted
    assert compose_validator_key("0x" + "a" * 63) in persisted


def test_v2_unknown_kind_prefix_ignored():
    """Defensive: a future kind prefix the server doesn't know about
    must not crash or pollute ``display_order``."""
    doc = _doc()
    new = reorder_tracking_doc_v2(
        doc,
        order=[
            "alien:0xdeadbeef",
            compose_validator_key("0x" + "a" * 63),
        ],
    )
    persisted = new.get("display_order") or []
    assert "alien:0xdeadbeef" not in persisted
    assert compose_validator_key("0x" + "a" * 63) in persisted


def test_v2_dedupes_repeated_keys():
    """A buggy client that sends the same key twice can't double-render
    the entry. The server keeps only the first occurrence."""
    doc = _doc()
    key_a = compose_validator_key("0x" + "a" * 63)
    new = reorder_tracking_doc_v2(doc, order=[key_a, key_a, key_a])
    persisted = new.get("display_order") or []
    assert persisted == [key_a]


def test_v2_does_not_mutate_input():
    doc = _doc()
    snap = copy.deepcopy(doc)
    reorder_tracking_doc_v2(doc, order=[compose_validator_key("0x" + "c" * 63)])
    assert doc == snap


def test_v2_empty_order_clears_display_order():
    """``order=[]`` (or all-stale) should remove the field, not leave
    an empty list lying around — keeps the doc canonical."""
    doc = _doc()
    doc["display_order"] = [compose_validator_key("0x" + "a" * 63)]
    new = reorder_tracking_doc_v2(doc, order=[])
    assert "display_order" not in new


def test_normalize_preserves_display_order_through_dump_load():
    """Critical invariant: ``dump_tracking → load_tracking`` round-trips
    ``display_order`` intact. Before the fix, ``_normalize`` dropped
    unknown top-level keys silently — making ``display_order`` impossible
    to persist."""
    from services.tracking_service import dump_tracking, load_tracking

    doc = _doc()
    doc["display_order"] = [
        compose_validator_key("0x" + "c" * 63),
        compose_delegation_key("0x" + "2" * 63, "0x" + "b" * 63),
    ]
    round_tripped = load_tracking(dump_tracking(doc))
    assert round_tripped.get("display_order") == doc["display_order"]


def test_normalize_drops_malformed_display_order():
    """Bad shapes (non-list, list of non-strings) get dropped silently
    — defends against a corrupt blob that would otherwise crash the
    renderer downstream."""
    from services.tracking_service import _normalize

    doc1 = {"validators": [], "delegations": [], "display_order": "not-a-list"}
    assert "display_order" not in _normalize(doc1)

    doc2 = {"validators": [], "delegations": [], "display_order": [1, 2, 3]}
    assert "display_order" not in _normalize(doc2)


def test_prune_display_order_removes_dangling_keys():
    """After a removal that didn't touch ``display_order`` (e.g. a PUT
    /tracking that replaced the arrays), ``_prune_display_order`` drops
    keys that no longer resolve. Empty-after-pruning → field is dropped
    entirely so the natural-order fallback kicks in cleanly."""
    from services.tracking_service import _prune_display_order

    # Doc has only validator ``a``; display_order references c (removed)
    # and a (still here).
    doc = {
        "validators": [{"address": "0x" + "a" * 63, "label": ""}],
        "delegations": [],
        "display_order": [
            compose_validator_key("0x" + "c" * 63),
            compose_validator_key("0x" + "a" * 63),
        ],
    }
    _prune_display_order(doc)
    assert doc["display_order"] == [compose_validator_key("0x" + "a" * 63)]

    # All-stale → field dropped.
    doc2 = {
        "validators": [{"address": "0x" + "a" * 63, "label": ""}],
        "delegations": [],
        "display_order": [compose_validator_key("0x" + "z" * 63)],
    }
    _prune_display_order(doc2)
    assert "display_order" not in doc2


def test_v2_normalizes_mixed_case_keys():
    """A client that capitalizes the hex part of a key (or the prefix)
    still matches the canonical lower-case form. Belt-and-braces against
    a Mini-App build that forgets ``.toLowerCase()``."""
    doc = _doc()
    upper_key = "validator:0x" + "A" * 63
    new = reorder_tracking_doc_v2(doc, order=[upper_key])
    persisted = new.get("display_order") or []
    assert persisted == [compose_validator_key("0x" + "a" * 63)]


# ---------------------------------------------------------------------------
# fetch_tracking_entries: ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_entries_respects_display_order(monkeypatch):
    """The renderer's order must come from ``display_order`` when
    present. We stub the staking-service RPC calls so the test stays
    hermetic — only the ordering logic matters here."""
    from services import tracking_service as ts
    from services.tracking_service import dump_tracking, fetch_tracking_entries

    async def _fake_validator_info(addr, with_attestation=True):  # noqa: ARG001
        return None

    async def _fake_delegator_positions(staker, delegator):  # noqa: ARG001
        return None

    # Patch in tracking_service's namespace because that module did
    # ``from services.staking_service import get_validator_info`` at
    # load time — the binding lives in tracking_service, not
    # staking_service, so patching the source module wouldn't take.
    monkeypatch.setattr(ts, "get_validator_info", _fake_validator_info)
    monkeypatch.setattr(ts, "get_delegator_positions", _fake_delegator_positions)

    doc = _doc()
    # Cross-group: delegation between two validators.
    doc["display_order"] = [
        compose_validator_key("0x" + "c" * 63),
        compose_delegation_key("0x" + "2" * 63, "0x" + "b" * 63),
        compose_validator_key("0x" + "a" * 63),
    ]
    entries = await fetch_tracking_entries(dump_tracking(doc))
    # Stable keys derived from the resolved entries should match the
    # display_order, plus the unmentioned entries appended at the end.
    keys = []
    for e in entries:
        if e.kind == "validator":
            keys.append(compose_validator_key(e.address))
        else:
            keys.append(compose_delegation_key(e.address, e.pool))
    assert keys[:3] == doc["display_order"]
    # Two entries weren't mentioned (validator b, delegation 1↔a) — they
    # follow at the end in natural order.
    assert compose_validator_key("0x" + "b" * 63) in keys[3:]
    assert compose_delegation_key("0x" + "1" * 63, "0x" + "a" * 63) in keys[3:]


@pytest.mark.asyncio
async def test_fetch_entries_falls_back_when_display_order_absent(monkeypatch):
    """No ``display_order`` field → natural order (validators first,
    delegations second). Backward-compat for users who haven't reordered
    since the cross-group feature shipped."""
    from services import tracking_service as ts
    from services.tracking_service import dump_tracking, fetch_tracking_entries

    async def _fake_validator_info(addr, with_attestation=True):  # noqa: ARG001
        return None

    async def _fake_delegator_positions(staker, delegator):  # noqa: ARG001
        return None

    # Same namespace fix as the cross-group test — patch the binding
    # inside tracking_service, not the source module.
    monkeypatch.setattr(ts, "get_validator_info", _fake_validator_info)
    monkeypatch.setattr(ts, "get_delegator_positions", _fake_delegator_positions)

    doc = _doc()  # no display_order field
    entries = await fetch_tracking_entries(dump_tracking(doc))
    kinds = [e.kind for e in entries]
    # Validators before delegations.
    assert kinds == ["validator"] * 3 + ["delegator"] * 2


# ---------------------------------------------------------------------------
# Pydantic ReorderPayload — mutual-exclusion + flat shape acceptance
# ---------------------------------------------------------------------------


def test_pydantic_reorder_payload_accepts_flat_order():
    from api.routers.users import ReorderPayload

    p = ReorderPayload(
        order=[compose_validator_key("0x" + "a" * 63)]
    )
    assert p.order == [compose_validator_key("0x" + "a" * 63)]
    assert p.validators is None
    assert p.delegations is None


def test_pydantic_reorder_payload_accepts_legacy_two_list():
    from api.routers.users import ReorderPayload

    p = ReorderPayload(
        validators=["0x" + "a" * 63],
        delegations=[("0x" + "1" * 63, "0x" + "a" * 63)],
    )
    assert p.order is None
    assert p.validators == ["0x" + "a" * 63]


def test_pydantic_reorder_payload_rejects_mixed_shapes():
    """``order`` AND ``validators``/``delegations`` together → 422 via
    pydantic ValidationError. Surfaces as ``conflicting_payload``."""
    import pytest as _pt
    from pydantic import ValidationError

    from api.routers.users import ReorderPayload

    with _pt.raises(ValidationError) as exc:
        ReorderPayload(
            order=[compose_validator_key("0x" + "a" * 63)],
            validators=["0x" + "a" * 63],
        )
    assert "conflicting_payload" in str(exc.value)


def test_pydantic_reorder_payload_rejects_order_with_delegations():
    """Same exclusivity rule applies to ``order`` + ``delegations``."""
    import pytest as _pt
    from pydantic import ValidationError

    from api.routers.users import ReorderPayload

    with _pt.raises(ValidationError) as exc:
        ReorderPayload(
            order=[compose_delegation_key("0x" + "1" * 63, "0x" + "a" * 63)],
            delegations=[("0x" + "1" * 63, "0x" + "a" * 63)],
        )
    assert "conflicting_payload" in str(exc.value)
