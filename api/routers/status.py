"""``GET /api/v1/status`` — health + protocol state overview."""
from __future__ import annotations

from fastapi import APIRouter

from services.staking_dto import NodeSync, StakingSystemInfo
from services.staking_service import fetch_node_sync, fetch_system_info

router = APIRouter(prefix="/api/v1", tags=["status"])


@router.get("/status", response_model=StakingSystemInfo, summary="Service & protocol status")
async def status_endpoint() -> StakingSystemInfo:
    return await fetch_system_info()


@router.get(
    "/status/node",
    response_model=NodeSync | None,
    summary="Sync state of the RPC node (cheap; safe to poll)",
)
async def node_sync_endpoint() -> NodeSync | None:
    """One RPC probe, no contract calls.

    Split out from ``/status`` so the Mini App header can refresh the
    sync dot every few seconds without re-reading protocol parameters.
    """
    return await fetch_node_sync()
