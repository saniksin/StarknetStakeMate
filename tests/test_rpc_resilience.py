"""Regression tests for transient-RPC resilience (Mini App 500s).

Two layers, matching the 2026-07-14 incident where pathfinder briefly
lagged and answered every ``pre_confirmed`` call with ``-32603
pre-confirmed data unavailable: syncing``:

1. The shared client must query state at ``latest`` by default —
   pathfinder always serves ``latest``, even mid-sync.
2. ``fetch_tracking_entries`` must degrade one failing entry to
   ``data=None`` instead of letting the exception 500 the whole
   ``/users/me/entries`` response.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from starknet_py.net.client_errors import ClientError
from starknet_py.net.client_models import Call
from starknet_py.net.full_node_client import FullNodeClient

from services import tracking_service
from services.rpc_client import get_client
from services.tracking_service import fetch_tracking_entries

VALID_ADDR_A = "0x" + "a" * 63
VALID_ADDR_B = "0x" + "b" * 63
VALID_ADDR_C = "0x" + "c" * 63

_DUMMY_CALL = Call(to_addr=1, selector=2, calldata=[])


def _capture_call_contract(monkeypatch) -> dict:
    """Patch the base ``call_contract`` and record the kwargs it receives."""
    captured: dict = {}

    async def _fake(self, call, block_hash=None, block_number=None):  # noqa: ARG001
        captured["block_hash"] = block_hash
        captured["block_number"] = block_number
        return [0]

    monkeypatch.setattr(FullNodeClient, "call_contract", _fake, raising=True)
    return captured


@pytest.mark.asyncio
async def test_call_contract_defaults_to_latest(monkeypatch) -> None:
    captured = _capture_call_contract(monkeypatch)
    client = get_client()
    await client.call_contract(_DUMMY_CALL)
    assert captured["block_number"] == "latest"
    assert captured["block_hash"] is None


@pytest.mark.asyncio
async def test_call_contract_explicit_block_passes_through(monkeypatch) -> None:
    captured = _capture_call_contract(monkeypatch)
    client = get_client()
    await client.call_contract(_DUMMY_CALL, block_number=12345)
    assert captured["block_number"] == 12345

    await client.call_contract(_DUMMY_CALL, block_hash="pre_confirmed")
    assert captured["block_hash"] == "pre_confirmed"
    assert captured["block_number"] is None


@pytest.mark.asyncio
async def test_fetch_entries_isolates_failing_entry(monkeypatch) -> None:
    """One entry whose RPC lookup blows up must not take down the rest."""
    sentinel = SimpleNamespace(kind="delegator-data")

    async def _boom(addr, **kwargs):  # noqa: ARG001
        raise ClientError(
            "Internal error. Data: {'error': 'pre-confirmed data unavailable: syncing'}"
        )

    async def _ok(staker, delegator, **kwargs):  # noqa: ARG001
        return sentinel

    monkeypatch.setattr(tracking_service, "get_validator_info", _boom, raising=True)
    monkeypatch.setattr(tracking_service, "get_delegator_positions", _ok, raising=True)

    doc = (
        '{"validators": [{"address": "%s", "label": "V"}],'
        ' "delegations": [{"delegator": "%s", "staker": "%s", "label": "D"}]}'
        % (VALID_ADDR_A, VALID_ADDR_B, VALID_ADDR_C)
    )

    entries = await fetch_tracking_entries(doc)

    assert len(entries) == 2
    validator = next(e for e in entries if e.kind == "validator")
    delegator = next(e for e in entries if e.kind == "delegator")
    assert validator.data is None  # degraded, not raised
    assert delegator.data is sentinel  # unaffected neighbour


@pytest.mark.asyncio
async def test_fetch_entries_all_failing_still_returns_rows(monkeypatch) -> None:
    async def _boom(*args, **kwargs):  # noqa: ARG001
        raise ClientError("Internal error")

    monkeypatch.setattr(tracking_service, "get_validator_info", _boom, raising=True)
    monkeypatch.setattr(
        tracking_service, "get_delegator_positions", _boom, raising=True
    )

    doc = '{"validators": [{"address": "%s", "label": "V"}], "delegations": []}' % (
        VALID_ADDR_A
    )
    entries = await fetch_tracking_entries(doc)
    assert len(entries) == 1
    assert entries[0].data is None
