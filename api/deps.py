"""Shared FastAPI dependencies.

Right now this is just the ``?network=`` selector every data endpoint
accepts. It lives here rather than in each router so the validation
rules — and the error the client sees when Sepolia isn't configured —
exist in exactly one place.
"""
from __future__ import annotations

from fastapi import HTTPException, Query, status

from data.contracts import (
    DEFAULT_NETWORK,
    Network,
    UnknownNetworkError,
    available_networks,
    is_network_available,
    resolve_network,
)


def network_param(
    network: str | None = Query(
        default=None,
        description=(
            "Which chain to read. 'mainnet' (default) or 'sepolia' "
            "(alias: 'testnet'). Only networks with an RPC endpoint "
            "configured are served."
        ),
    ),
) -> Network:
    """Resolve and validate the ``?network=`` query parameter.

    Two distinct failures, two distinct statuses:

    - an unknown name is the caller's mistake → 400.
    - a known name with no endpoint configured is *our* missing
      configuration → 503, with the list of what we do serve, so the
      Mini App can hide the tab instead of retrying forever.
    """
    try:
        resolved = resolve_network(network)
    except UnknownNetworkError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "unknown_network", "message": str(exc)},
        ) from exc
    if not is_network_available(resolved):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "network_unavailable",
                "message": f"no RPC endpoint configured for {resolved}",
                "available": available_networks(),
            },
        )
    return resolved


__all__ = ["network_param", "DEFAULT_NETWORK"]
