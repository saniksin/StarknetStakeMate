"""Unit tests for the Mini-App add-flow service helpers.

These cover :func:`services.tracking_service.add_validator_to_tracking`
and :func:`services.tracking_service.add_delegator_to_tracking` — the
two new functions the bot and the API both call. Network calls
(``get_validator_info`` / ``get_delegator_positions``) are
monkeypatched so the tests run hermetically.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services import tracking_service
from services.tracking_service import (
    AddTrackingError,
    MAX_TRACKED_ENTRIES,
    add_delegator_to_tracking,
    add_validator_to_tracking,
)


VALID_ADDR_A = "0x" + "a" * 63
VALID_ADDR_B = "0x" + "b" * 63
VALID_ADDR_C = "0x" + "c" * 63


@pytest.mark.asyncio
async def test_add_validator_invalid_address(monkeypatch) -> None:
    with pytest.raises(AddTrackingError) as exc:
        await add_validator_to_tracking({}, address="not-a-hex")
    assert exc.value.code == "invalid_address"


@pytest.mark.asyncio
async def test_add_validator_happy_path(monkeypatch) -> None:
    async def _fake_info(addr, with_attestation=False):  # noqa: ARG001
        return SimpleNamespace(some_field=True)

    monkeypatch.setattr(
        "services.staking_service.get_validator_info", _fake_info, raising=True
    )

    doc = {"validators": [], "delegations": []}
    out, entry = await add_validator_to_tracking(
        doc, address=VALID_ADDR_A, label="Alice"
    )
    assert entry == {"address": VALID_ADDR_A, "label": "Alice"}
    assert len(out["validators"]) == 1
    assert out["validators"][0]["address"] == VALID_ADDR_A


@pytest.mark.asyncio
async def test_add_validator_label_truncated_to_40(monkeypatch) -> None:
    async def _fake_info(addr, with_attestation=False):  # noqa: ARG001
        return SimpleNamespace()

    monkeypatch.setattr(
        "services.staking_service.get_validator_info", _fake_info, raising=True
    )

    long_label = "x" * 200
    _, entry = await add_validator_to_tracking(
        {}, address=VALID_ADDR_A, label=long_label
    )
    assert len(entry["label"]) == 40
    assert entry["label"] == "x" * 40


@pytest.mark.asyncio
async def test_add_validator_duplicate_case_insensitive(monkeypatch) -> None:
    async def _fake_info(addr, with_attestation=False):  # noqa: ARG001
        return SimpleNamespace()

    monkeypatch.setattr(
        "services.staking_service.get_validator_info", _fake_info, raising=True
    )

    upper = "0x" + "A" * 63
    lower = "0x" + "a" * 63
    doc = {"validators": [{"address": upper, "label": ""}], "delegations": []}
    with pytest.raises(AddTrackingError) as exc:
        await add_validator_to_tracking(doc, address=lower)
    assert exc.value.code == "duplicate"


@pytest.mark.asyncio
async def test_add_validator_not_a_staker(monkeypatch) -> None:
    async def _fake_info(addr, with_attestation=False):  # noqa: ARG001
        return None

    monkeypatch.setattr(
        "services.staking_service.get_validator_info", _fake_info, raising=True
    )

    with pytest.raises(AddTrackingError) as exc:
        await add_validator_to_tracking({}, address=VALID_ADDR_A)
    assert exc.value.code == "not_a_staker"


@pytest.mark.asyncio
async def test_add_validator_capacity(monkeypatch) -> None:
    async def _fake_info(addr, with_attestation=False):  # noqa: ARG001
        return SimpleNamespace()

    monkeypatch.setattr(
        "services.staking_service.get_validator_info", _fake_info, raising=True
    )

    # Stuff the doc with MAX entries to exhaust capacity.
    doc = {
        "validators": [
            {"address": f"0x{i:063x}", "label": ""}
            for i in range(MAX_TRACKED_ENTRIES)
        ],
        "delegations": [],
    }
    with pytest.raises(AddTrackingError) as exc:
        await add_validator_to_tracking(doc, address=VALID_ADDR_A)
    assert exc.value.code == "limit_reached"


@pytest.mark.asyncio
async def test_add_delegator_happy_path(monkeypatch) -> None:
    async def _fake_positions(staker, delegator):  # noqa: ARG001
        return SimpleNamespace(has_any=True, positions=[])

    monkeypatch.setattr(
        "services.staking_service.get_delegator_positions",
        _fake_positions,
        raising=True,
    )

    doc = {"validators": [], "delegations": []}
    out, entry = await add_delegator_to_tracking(
        doc, delegator=VALID_ADDR_A, staker=VALID_ADDR_B, label="my pool"
    )
    assert entry == {
        "delegator": VALID_ADDR_A,
        "staker": VALID_ADDR_B,
        "label": "my pool",
    }
    assert len(out["delegations"]) == 1


@pytest.mark.asyncio
async def test_add_delegator_not_a_delegator(monkeypatch) -> None:
    async def _fake_positions(staker, delegator):  # noqa: ARG001
        return SimpleNamespace(has_any=False, positions=[])

    monkeypatch.setattr(
        "services.staking_service.get_delegator_positions",
        _fake_positions,
        raising=True,
    )

    with pytest.raises(AddTrackingError) as exc:
        await add_delegator_to_tracking(
            {}, delegator=VALID_ADDR_A, staker=VALID_ADDR_B
        )
    assert exc.value.code == "not_a_delegator"


@pytest.mark.asyncio
async def test_add_delegator_duplicate_pair(monkeypatch) -> None:
    async def _fake_positions(staker, delegator):  # noqa: ARG001
        return SimpleNamespace(has_any=True, positions=[])

    monkeypatch.setattr(
        "services.staking_service.get_delegator_positions",
        _fake_positions,
        raising=True,
    )

    doc = {
        "validators": [],
        "delegations": [
            # Stored in mixed case to exercise the case-insensitive
            # comparison: the Starknet hex regex accepts both upper- and
            # lower-case after the (lower-case) ``0x`` prefix, so users
            # can paste either and we must dedupe accordingly.
            {
                "delegator": "0x" + "A" * 63,
                "staker": "0x" + "B" * 63,
                "label": "",
            }
        ],
    }
    with pytest.raises(AddTrackingError) as exc:
        await add_delegator_to_tracking(
            doc, delegator=VALID_ADDR_A, staker=VALID_ADDR_B
        )
    assert exc.value.code == "duplicate"


@pytest.mark.asyncio
async def test_add_delegator_invalid_addresses(monkeypatch) -> None:
    with pytest.raises(AddTrackingError) as exc:
        await add_delegator_to_tracking(
            {}, delegator="not-hex", staker=VALID_ADDR_A
        )
    assert exc.value.code == "invalid_address"

    with pytest.raises(AddTrackingError) as exc:
        await add_delegator_to_tracking(
            {}, delegator=VALID_ADDR_A, staker="not-hex"
        )
    assert exc.value.code == "invalid_address"


@pytest.mark.asyncio
async def test_add_delegator_capacity_counts_both_lists(monkeypatch) -> None:
    """``MAX_TRACKED_ENTRIES`` is the *total* across validators+delegations."""

    async def _fake_positions(staker, delegator):  # noqa: ARG001
        return SimpleNamespace(has_any=True, positions=[])

    monkeypatch.setattr(
        "services.staking_service.get_delegator_positions",
        _fake_positions,
        raising=True,
    )

    half = MAX_TRACKED_ENTRIES // 2
    doc = {
        "validators": [
            {"address": f"0x{i:063x}", "label": ""} for i in range(half)
        ],
        "delegations": [
            {
                "delegator": f"0x{i:063x}",
                "staker": f"0x{i + 100:063x}",
                "label": "",
            }
            for i in range(MAX_TRACKED_ENTRIES - half)
        ],
    }
    with pytest.raises(AddTrackingError) as exc:
        await add_delegator_to_tracking(
            doc, delegator=VALID_ADDR_A, staker=VALID_ADDR_B
        )
    assert exc.value.code == "limit_reached"


def test_max_tracked_entries_constant() -> None:
    """Sanity check: bot's ``_MAX_TRACKED`` and the locale message both
    quote 10. If we ever raise the ceiling, the locale strings need
    updating to match."""
    assert MAX_TRACKED_ENTRIES == 10
    # Spot-check the locale messages reference 10 explicitly.
    import json
    from pathlib import Path

    en = json.loads(
        Path("locales/en.json").read_text(encoding="utf-8")
    )
    assert "10" in en["webapp_add_error_limit_reached"]
