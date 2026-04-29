"""Attestation contract bindings (Staking V2).

In V2 validators must attest a target block each epoch; missing it hurts
their rewards. ``get_last_epoch_attestation_done(staker)`` returns the
latest epoch number where attestation was recorded; the current epoch
minus that number (minus one, because the current epoch is still open) is
the miss count.

This module also exposes the *block-level* attestation primitives used by
the dashboard's extended status banner:

  - ``fetch_target_attestation_block(operational)``: the assigned block a
    validator must sign for in the current epoch (revealed at epoch start
    from staker stake + RNG, hence operator-keyed, NOT staker-keyed).
  - ``fetch_attestation_window()``: the window length in blocks (mainnet
    is currently 60); cached process-wide with a generous TTL because the
    parameter only changes via governance.
  - ``fetch_current_block_number()``: thin wrapper over the RPC client so
    callers don't have to import starknet-py directly.
"""
from __future__ import annotations

import asyncio
import time
from functools import lru_cache

from starknet_py.contract import Contract
from starknet_py.net.client_errors import ClientError

from data.contracts import get_network_addresses, load_abi
from services.rpc_client import get_client, with_retry
from services.staking_dto import AttestationStatus


@lru_cache(maxsize=1)
def _attestation_contract() -> Contract:
    addrs = get_network_addresses()
    return Contract(
        address=int(addrs.attestation_contract, 16),
        abi=load_abi("l2_attestation_contract"),
        provider=get_client(),
    )


async def fetch_last_epoch_attested(staker_address: str) -> int:
    contract = _attestation_contract()

    async def _call() -> int:
        (result,) = await contract.functions["get_last_epoch_attestation_done"].call(
            int(staker_address, 16)
        )
        return int(result)

    try:
        return await with_retry(
            _call, description=f"get_last_epoch_attestation_done({staker_address})"
        )
    except ClientError:
        # Stakers that never attested produce a "not found" style error; treat
        # them as "epoch zero" so missed-count still reflects the gap.
        return 0


async def fetch_is_attesting_this_epoch(staker_address: str) -> bool:
    contract = _attestation_contract()

    async def _call() -> bool:
        (result,) = await contract.functions["is_attestation_done_in_curr_epoch"].call(
            int(staker_address, 16)
        )
        return bool(result)

    try:
        return await with_retry(
            _call, description=f"is_attestation_done_in_curr_epoch({staker_address})"
        )
    except ClientError:
        return False


# ---------------------------------------------------------------------------
# Block-level primitives (used by the extended dashboard status)
# ---------------------------------------------------------------------------

# Cache for ``attestation_window()``. Governance can rotate this via
# ``set_attestation_window``, but it's a near-static parameter — caching
# for one hour avoids spamming the RPC on every dashboard render. The
# cache is keyed by RPC node address (via the FullNodeClient identity) so
# a process that ever talks to two networks doesn't mix them up.
_ATTESTATION_WINDOW_TTL_SECONDS = 3600
_attestation_window_cache: dict[str, tuple[int, float]] = {}


async def fetch_attestation_window() -> int | None:
    """Return the current attestation window length, in blocks.

    Cached for an hour. ``None`` if the RPC call fails — callers should
    fall back to a no-block-info banner rather than crashing.
    """
    contract = _attestation_contract()
    cache_key = str(getattr(contract.client, "url", "default"))
    now = time.monotonic()
    cached = _attestation_window_cache.get(cache_key)
    if cached is not None:
        value, expires_at = cached
        if now < expires_at:
            return value

    async def _call() -> int:
        (result,) = await contract.functions["attestation_window"].call()
        return int(result)

    try:
        window = await with_retry(_call, description="attestation_window()")
    except (ClientError, asyncio.TimeoutError):
        return None
    _attestation_window_cache[cache_key] = (
        window,
        now + _ATTESTATION_WINDOW_TTL_SECONDS,
    )
    return window


async def fetch_target_attestation_block(operational_address: str) -> int | None:
    """Return the assigned block for the validator's operator wallet in
    the current epoch.

    The contract method is keyed on the *operational* (operator) address,
    not the staker address — block selection mixes the validator's stake
    proof with an RNG and is published per-operator. ``None`` when the
    target hasn't been computed yet (very early in the epoch) or the
    operator isn't registered as an attester.
    """
    contract = _attestation_contract()

    async def _call() -> int:
        (result,) = await contract.functions[
            "get_current_epoch_target_attestation_block"
        ].call(int(operational_address, 16))
        return int(result)

    try:
        target = await with_retry(
            _call,
            description=f"get_current_epoch_target_attestation_block({operational_address})",
        )
    except (ClientError, asyncio.TimeoutError):
        return None
    # Contract returns 0 when the target is not yet defined; treat that
    # like "unavailable" so the renderer skips the block-info row instead
    # of showing a misleading "Assigned block: 0".
    if target <= 0:
        return None
    return target


async def fetch_current_block_number() -> int | None:
    """Latest block number on the configured RPC. ``None`` on RPC failure."""
    client = get_client()

    async def _call() -> int:
        return int(await client.get_block_number())

    try:
        return await with_retry(_call, description="get_block_number()")
    except (ClientError, asyncio.TimeoutError):
        return None


# ---------------------------------------------------------------------------
# Composed status DTO
# ---------------------------------------------------------------------------


async def fetch_attestation_status(
    staker_address: str,
    *,
    current_epoch: int,
    operational_address: str | None = None,
) -> AttestationStatus:
    """Compose :class:`AttestationStatus` for the staker.

    When ``operational_address`` is provided we additionally fetch the
    block-level extras (target / window / current block) so the dashboard
    can render the extended waiting banner. All four extra calls run in
    parallel; any individual one failing produces ``None`` for that
    field, which the renderer falls back to a shorter banner from.

    We intentionally still fetch the block-level extras even when
    ``is_attesting_this_epoch=True`` — the renderer uses ``current_block``
    to compute the epoch-tail "next epoch in N blocks" line that's shown
    in every status state, not just waiting.
    """
    last_done_t = fetch_last_epoch_attested(staker_address)
    attested_now_t = fetch_is_attesting_this_epoch(staker_address)
    if operational_address and operational_address != "0x0":
        target_t = fetch_target_attestation_block(operational_address)
    else:
        async def _none() -> None:
            return None
        target_t = _none()
    window_t = fetch_attestation_window()
    current_block_t = fetch_current_block_number()

    last_done, attested_now, target_block, window, current_block = await asyncio.gather(
        last_done_t,
        attested_now_t,
        target_t,
        window_t,
        current_block_t,
    )
    # The "closed" epochs the staker should have attested are
    # [1 .. current_epoch - 1]. Anything between last_done+1 and current_epoch-1
    # is missed. Negative values -> zero.
    missed = max(0, current_epoch - 1 - last_done)
    return AttestationStatus(
        last_epoch_attested=last_done,
        current_epoch=current_epoch,
        missed_epochs=missed,
        is_attesting_this_epoch=attested_now,
        target_block=target_block,
        attestation_window_blocks=window,
        current_block=current_block,
    )
