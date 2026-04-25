"""Resilient Starknet RPC client wrapper.

Adds retry with exponential backoff around transient RPC failures while
letting domain errors (invalid address, staker-not-exists) pass through
immediately.
"""
from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any, Awaitable, Callable, TypeVar

from loguru import logger
from starknet_py.net.client_errors import ClientError
from starknet_py.net.full_node_client import FullNodeClient
from starknet_py.serialization.errors import InvalidValueException
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from data.contracts import STARKNET_RPC_URL

T = TypeVar("T")


@lru_cache(maxsize=1)
def get_client() -> FullNodeClient:
    """Return a process-wide singleton FullNodeClient.

    Read-only calls do not need an Account, despite what the legacy code
    implied; an Account was only required because starknet-py 0.24 refuses
    to build a Contract without a provider that has a chain id.
    """
    return FullNodeClient(node_url=STARKNET_RPC_URL)


async def with_retry(
    op: Callable[[], Awaitable[T]],
    *,
    description: str,
    attempts: int = 3,
) -> T:
    """Run an async operation with bounded exponential-backoff retries.

    Only retries on :class:`ClientError` and :class:`asyncio.TimeoutError`.
    Domain errors such as :class:`InvalidValueException` (e.g. calling
    ``get_staker_info_v1`` on a non-existent staker) propagate immediately.
    """

    async def _runner() -> T:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=0.5, max=4),
            retry=retry_if_exception_type((ClientError, asyncio.TimeoutError)),
            reraise=True,
        ):
            with attempt:
                return await op()
        raise RuntimeError("unreachable")  # pragma: no cover

    try:
        return await _runner()
    except (ClientError, asyncio.TimeoutError) as exc:
        logger.error(f"RPC failure after retries ({description}): {exc}")
        raise
    except InvalidValueException:
        # Explicit propagation for readability; not retried.
        raise


def is_missing_contract_error(exc: BaseException) -> bool:
    """True if the exception indicates the callee address has no contract."""
    if isinstance(exc, ClientError):
        msg = str(exc).lower()
        return "contract not found" in msg or "contract_not_found" in msg
    return False


# Revert reasons that the staking contracts return for "the thing you asked
# about doesn't exist". These are deterministic domain errors — retrying just
# wastes 6+ seconds of exponential backoff without ever changing the answer.
_DOMAIN_REVERT_MARKERS = (
    "staker does not exist",
    "pool member does not exist",
    "delegator does not exist",
    "pool does not exist",
)


def is_domain_revert(exc: BaseException) -> bool:
    """True for deterministic ‘not found’ reverts from staking contracts.

    Callers should treat these like ``None`` (no data) and skip the retry
    loop. ``with_retry`` does NOT auto-skip them because some operators wrap
    the same error class for transient failures; the caller's ``_call`` is
    the right place to map domain revert → ``None``.
    """
    if isinstance(exc, ClientError):
        msg = str(exc).lower()
        return any(marker in msg for marker in _DOMAIN_REVERT_MARKERS)
    return False
