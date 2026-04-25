"""Backwards-compatible shim around :mod:`services.staking_service`.

Older code (tasks, some handlers) imports ``parse_validator_staking_info`` and
``parse_delegator_staking_info`` as if they returned raw starknet-py tuples.
After the Staking V2 upgrade the raw ABI changed (see the design doc), so we
now expose typed DTOs directly instead. Callers should migrate to importing
from :mod:`services.staking_service`; this module remains only to avoid a
flag-day refactor across every handler.
"""
from __future__ import annotations

from services.staking_dto import DelegatorInfo, ValidatorInfo
from services.staking_service import get_delegator_info, get_validator_info


async def parse_validator_staking_info(validator_address: str) -> ValidatorInfo | None:
    """Return the full V2 validator view; ``None`` if the staker is unknown."""
    return await get_validator_info(validator_address)


async def parse_delegator_staking_info(
    delegator_address: str, pool_address: str
) -> DelegatorInfo | None:
    """Return the delegator's view in the given pool; ``None`` if absent."""
    return await get_delegator_info(pool_address, delegator_address)
