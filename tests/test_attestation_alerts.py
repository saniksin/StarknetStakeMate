"""Per-epoch gas-alert tests for ``tasks.attestation_alerts``.

The new contract (2026-04): operator-balance alerts fire AT MOST once
per epoch boundary. The watcher still ticks every minute (attestation
health needs sub-minute SLA), but the balance-RPC + alert-send branch
is gated on ``epoch_changed=True``. State machine:

    was_below | is_below | message
    ----------+----------+--------------
    False     | False    | silent (no DB write)
    False     | True     | low-balance alert  (set was_below=True)
    True      | True     | low-balance alert  (already True, no DB write)
    True      | False    | recovered alert    (clear was_below)

These tests freeze that table by mocking out the RPC + Telegram
sendMessage layers; they don't touch the actual network.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from db_api.models import Users
from tasks.attestation_alerts import (
    _check_user,
    _reset_last_seen_epoch_for_tests,
)


# ---------------------------------------------------------------------------
# Test fixtures: a Users row carrying one tracked validator and the
# operator-balance alert configured.
# ---------------------------------------------------------------------------


STAKER = "0x" + "a" * 64
OP_ADDR = "0x" + "c" * 64


def _make_user(*, was_below: bool, balance_min: float = 10.0) -> Users:
    """Build an in-memory Users row with the operator-balance alert on."""
    user = Users(
        user_id=42, user_name="alice", user_language="en", registration_data=None
    )
    user.tracking_data = json.dumps(
        {"validators": [{"address": STAKER, "label": "Karnot"}], "delegations": []}
    )
    cfg = {
        "operator_balance_min_strk": balance_min,
        "attestation_alerts_for": [STAKER],
        "_attestation_state": {},
        "_operator_balance_was_below": {STAKER: True} if was_below else {},
    }
    user.set_notification_config(cfg)
    return user


@pytest.fixture(autouse=True)
def _reset_epoch_cursor() -> None:
    """Each test starts with a clean ``_last_seen_epoch`` so we control
    the ``epoch_changed`` flag explicitly."""
    _reset_last_seen_epoch_for_tests()
    yield
    _reset_last_seen_epoch_for_tests()


# ---------------------------------------------------------------------------
# State-machine cases. We mock out fetch_strk_balance + fetch_staker_raw
# + fetch_attestation_status + the Telegram sender, then call _check_user
# directly with the epoch_changed flag we want to exercise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_balance_first_epoch_fires_alert() -> None:
    """was=False, is=True (epoch boundary) → low-balance alert is sent."""
    user = _make_user(was_below=False)
    with (
        patch(
            "tasks.attestation_alerts.fetch_strk_balance",
            new=AsyncMock(return_value=Decimal("3.42")),
        ),
        patch(
            "tasks.attestation_alerts.fetch_staker_raw",
            new=AsyncMock(return_value={"operational_address": int(OP_ADDR, 16)}),
        ),
        patch(
            "tasks.attestation_alerts.fetch_attestation_status",
            new=AsyncMock(return_value=None),
        ),
        patch("tasks.attestation_alerts._send", new=AsyncMock()) as mock_send,
    ):
        att, bal = await _check_user(user, current_epoch=9590, epoch_changed=True)

    assert mock_send.await_count == 1
    msg = mock_send.await_args.args[1]
    assert "3.42 STRK" in msg
    assert "9590" in msg  # epoch in alert text
    assert bal == {STAKER: True}


@pytest.mark.asyncio
async def test_low_balance_same_below_state_repeats_in_new_epoch() -> None:
    """was=True, is=True (epoch boundary) → low-balance alert is sent
    again (one DM per epoch). State unchanged → no DB write needed."""
    user = _make_user(was_below=True)
    with (
        patch(
            "tasks.attestation_alerts.fetch_strk_balance",
            new=AsyncMock(return_value=Decimal("3.42")),
        ),
        patch(
            "tasks.attestation_alerts.fetch_staker_raw",
            new=AsyncMock(return_value={"operational_address": int(OP_ADDR, 16)}),
        ),
        patch(
            "tasks.attestation_alerts.fetch_attestation_status",
            new=AsyncMock(return_value=None),
        ),
        patch("tasks.attestation_alerts._send", new=AsyncMock()) as mock_send,
    ):
        _, bal = await _check_user(user, current_epoch=9591, epoch_changed=True)

    assert mock_send.await_count == 1
    # State unchanged → no targeted DB write
    assert bal is None


@pytest.mark.asyncio
async def test_recovered_alert_when_balance_back_above_threshold() -> None:
    """was=True, is=False (epoch boundary) → recovered alert + clear flag."""
    user = _make_user(was_below=True)
    with (
        patch(
            "tasks.attestation_alerts.fetch_strk_balance",
            new=AsyncMock(return_value=Decimal("25.00")),
        ),
        patch(
            "tasks.attestation_alerts.fetch_staker_raw",
            new=AsyncMock(return_value={"operational_address": int(OP_ADDR, 16)}),
        ),
        patch(
            "tasks.attestation_alerts.fetch_attestation_status",
            new=AsyncMock(return_value=None),
        ),
        patch("tasks.attestation_alerts._send", new=AsyncMock()) as mock_send,
    ):
        _, bal = await _check_user(user, current_epoch=9591, epoch_changed=True)

    assert mock_send.await_count == 1
    msg = mock_send.await_args.args[1]
    # The recovered template includes the new balance
    assert "25.00 STRK" in msg
    assert "9591" in msg
    # was_below cleared
    assert bal == {}


@pytest.mark.asyncio
async def test_silent_when_above_and_was_above() -> None:
    """was=False, is=False → silent. No alert, no DB write."""
    user = _make_user(was_below=False)
    with (
        patch(
            "tasks.attestation_alerts.fetch_strk_balance",
            new=AsyncMock(return_value=Decimal("25.00")),
        ),
        patch(
            "tasks.attestation_alerts.fetch_staker_raw",
            new=AsyncMock(return_value={"operational_address": int(OP_ADDR, 16)}),
        ),
        patch(
            "tasks.attestation_alerts.fetch_attestation_status",
            new=AsyncMock(return_value=None),
        ),
        patch("tasks.attestation_alerts._send", new=AsyncMock()) as mock_send,
    ):
        _, bal = await _check_user(user, current_epoch=9591, epoch_changed=True)

    assert mock_send.await_count == 0
    assert bal is None


@pytest.mark.asyncio
async def test_no_alert_outside_epoch_boundary() -> None:
    """epoch_changed=False → balance branch skipped entirely.

    Even if the balance is in the alert range, we don't fire — the watcher
    is supposed to deliver one DM per epoch boundary, not on every minute
    tick. The whole point of this rewrite was to stop spamming.
    """
    user = _make_user(was_below=False)
    fetch_balance = AsyncMock(return_value=Decimal("3.42"))
    with (
        patch("tasks.attestation_alerts.fetch_strk_balance", new=fetch_balance),
        patch(
            "tasks.attestation_alerts.fetch_staker_raw",
            new=AsyncMock(return_value={"operational_address": int(OP_ADDR, 16)}),
        ),
        patch(
            "tasks.attestation_alerts.fetch_attestation_status",
            new=AsyncMock(return_value=None),
        ),
        patch("tasks.attestation_alerts._send", new=AsyncMock()) as mock_send,
    ):
        _, bal = await _check_user(user, current_epoch=9590, epoch_changed=False)

    assert mock_send.await_count == 0
    # Crucially the RPC was NOT called either — saves bandwidth on every
    # non-boundary tick (and every user is processed in parallel, so the
    # savings multiply).
    assert fetch_balance.await_count == 0
    assert bal is None


@pytest.mark.asyncio
async def test_legacy_state_migrates_on_read() -> None:
    """Existing rows that still carry ``_operator_balance_state`` get
    auto-migrated to ``_operator_balance_was_below`` on the next read."""
    user = Users(
        user_id=99, user_name="bob", user_language="en", registration_data=None
    )
    user.tracking_data = json.dumps(
        {"validators": [{"address": STAKER, "label": "X"}], "delegations": []}
    )
    # Hand-craft the legacy JSON shape that was in the DB before the
    # rewrite. Not via set_notification_config — that would already
    # canonicalize.
    user.notification_config = json.dumps(
        {
            "usd_threshold": 0.0,
            "token_thresholds": {},
            "attestation_alerts_for": [STAKER],
            "_attestation_state": {},
            "operator_balance_min_strk": 10.0,
            "_operator_balance_state": {STAKER: 1},  # legacy "below" flag
        }
    )

    cfg = user.get_notification_config()
    assert cfg["_operator_balance_was_below"] == {STAKER: True}
    assert "_operator_balance_state" not in cfg
