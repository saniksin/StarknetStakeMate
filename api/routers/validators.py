"""``/api/v1/validators/{address}`` — validator view."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from api.deps import network_param
from data.contracts import Network
from services.staking_dto import ValidatorInfo, ValidatorUptime
from services.staking_service import get_validator_info
from services.uptime_service import fetch_validator_uptime
from utils.check_valid_addresses import is_valid_starknet_address

router = APIRouter(prefix="/api/v1/validators", tags=["validators"])


@router.get("/{address}", response_model=ValidatorInfo, summary="Validator status + pools")
async def get_validator(
    address: str = Path(..., description="Staker ContractAddress, hex"),
    with_attestation: bool = Query(True, description="Include missed-epoch attestation info"),
    network: Network = Depends(network_param),
) -> ValidatorInfo:
    if not is_valid_starknet_address(address):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid Starknet address")
    info = await get_validator_info(
        address, with_attestation=with_attestation, network=network
    )
    if info is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="validator not found on-chain")
    return info


@router.get(
    "/{address}/uptime",
    response_model=ValidatorUptime,
    summary="Validator attestation uptime (third-party index)",
)
async def get_validator_uptime(
    address: str = Path(..., description="Staker ContractAddress, hex"),
    network: Network = Depends(network_param),
) -> ValidatorUptime:
    """Uptime as published by Endur's staking indexer.

    Always 200 with a body, even when the upstream is down: the response
    carries a ``status`` of ``ok`` / ``not_indexed`` / ``unavailable`` so
    the Mini App can render "couldn't fetch this" rather than dropping
    the block. A third-party outage must not turn a validator card into
    an error page — nor make the feature disappear unnoticed. See
    :mod:`services.uptime_service` for why Voyager isn't used.
    """
    if not is_valid_starknet_address(address):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid Starknet address")
    return await fetch_validator_uptime(address, network=network)
