"""``GET /api/v1/status`` — health + protocol state overview."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.deps import network_param
from data.contracts import DEFAULT_NETWORK, Network, available_networks
from services.staking_dto import NodeSync, StakingSystemInfo
from services.staking_service import fetch_node_sync, fetch_system_info

router = APIRouter(prefix="/api/v1", tags=["status"])


class NetworksPayload(BaseModel):
    """Which chains this deployment can serve, and which one is the default.

    The Mini App reads this once on boot: with a single entry it draws no
    network switch at all, so a deployment without a Sepolia endpoint
    looks exactly like it did before testnet support existed.
    """

    networks: list[str] = Field(description="Configured networks, default first")
    default: str = Field(description="Network used when ?network= is omitted")


@router.get("/networks", response_model=NetworksPayload, summary="Configured networks")
async def networks_endpoint() -> NetworksPayload:
    return NetworksPayload(
        networks=list(available_networks()), default=DEFAULT_NETWORK
    )


@router.get("/status", response_model=StakingSystemInfo, summary="Service & protocol status")
async def status_endpoint(
    network: Network = Depends(network_param),
) -> StakingSystemInfo:
    return await fetch_system_info(network=network)


@router.get(
    "/status/node",
    response_model=NodeSync | None,
    summary="Sync state of the RPC node (cheap; safe to poll)",
)
async def node_sync_endpoint(
    network: Network = Depends(network_param),
) -> NodeSync | None:
    """One RPC probe, no contract calls.

    Split out from ``/status`` so the Mini App header can refresh the
    sync dot every few seconds without re-reading protocol parameters.
    Each network has its own node, so the dot follows the selected tab.
    """
    return await fetch_node_sync(network=network)
