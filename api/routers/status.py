"""``GET /api/v1/status`` — health + protocol state overview."""
from __future__ import annotations

from fastapi import APIRouter

from services.staking_dto import StakingSystemInfo
from services.staking_service import fetch_system_info

router = APIRouter(prefix="/api/v1", tags=["status"])


@router.get("/status", response_model=StakingSystemInfo, summary="Service & protocol status")
async def status_endpoint() -> StakingSystemInfo:
    return await fetch_system_info()
