"""Attestation contract bindings (Staking V2).

In V2 validators must attest a target block each epoch; missing it hurts
their rewards. ``get_last_epoch_attestation_done(staker)`` returns the
latest epoch number where attestation was recorded; the current epoch
minus that number (minus one, because the current epoch is still open) is
the miss count.
"""
from __future__ import annotations

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


async def fetch_attestation_status(
    staker_address: str, *, current_epoch: int
) -> AttestationStatus:
    """Compose :class:`AttestationStatus` for the staker."""
    import asyncio

    last_done, attested_now = await asyncio.gather(
        fetch_last_epoch_attested(staker_address),
        fetch_is_attesting_this_epoch(staker_address),
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
    )
