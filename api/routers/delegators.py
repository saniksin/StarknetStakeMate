"""``/api/v1/delegators/{address}`` — delegator position inside one pool."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Query, status

from services.staking_dto import DelegatorInfo
from services.staking_service import get_delegator_info
from utils.check_valid_addresses import is_valid_starknet_address

router = APIRouter(prefix="/api/v1/delegators", tags=["delegators"])


@router.get("/{address}", response_model=DelegatorInfo, summary="Delegator position")
async def get_delegator(
    address: str = Path(..., description="Delegator ContractAddress, hex"),
    pool: str = Query(..., description="Pool contract address, hex"),
) -> DelegatorInfo:
    if not is_valid_starknet_address(address):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid delegator address")
    if not is_valid_starknet_address(pool):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid pool address")
    info = await get_delegator_info(pool, address)
    if info is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="delegator not found in pool")
    return info
