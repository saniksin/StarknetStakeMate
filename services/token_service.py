"""Token metadata lookup (symbol + decimals) with in-process caching.

Starknet V2 lets validators host multiple pools, one per staking-eligible
token. To render balances correctly we need the ``decimals()`` and
``symbol()`` of each token. These rarely change, so we cache them in the
process.
"""
from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from functools import lru_cache
from typing import Iterable

from loguru import logger
from starknet_py.contract import Contract
from starknet_py.net.client_errors import ClientError

from services.rpc_client import get_client, with_retry
from services.staking_dto import TokenInfo

_TTL = int(os.getenv("TOKEN_CACHE_TTL", "3600"))

# Minimal fragment of the ERC-20 view interface that we need. starknet-py can
# parse it by itself; we hand-roll the ABI to avoid a round-trip for each token.
_ERC20_ABI = [
    {
        "type": "interface",
        "name": "IErc20Metadata",
        "items": [
            {
                "type": "function",
                "name": "symbol",
                "inputs": [],
                "outputs": [{"type": "core::felt252"}],
                "state_mutability": "view",
            },
            {
                "type": "function",
                "name": "decimals",
                "inputs": [],
                "outputs": [{"type": "core::integer::u8"}],
                "state_mutability": "view",
            },
            {
                "type": "function",
                "name": "balance_of",
                "inputs": [{"name": "account", "type": "core::starknet::contract_address::ContractAddress"}],
                "outputs": [{"type": "core::integer::u256"}],
                "state_mutability": "view",
            },
        ],
    }
]


# Mainnet STRK token. Hard-coded because operator-balance lookups need it
# constantly and we want to avoid a DB / config detour.
STRK_TOKEN_ADDRESS = "0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d"


@lru_cache(maxsize=1)
def _strk_contract() -> "Contract":
    """One cached starknet-py Contract for the STRK token (ABI parsed once)."""
    return Contract(
        address=int(STRK_TOKEN_ADDRESS, 16),
        abi=_ERC20_ABI,
        provider=get_client(),
    )


async def fetch_strk_balance(account_address: str) -> Decimal:
    """Return ``account``'s on-chain STRK balance, scaled to whole tokens.

    Used for the operator-wallet low-balance alert: validators must keep
    a small STRK reserve to pay attestation gas, and running dry causes
    silent missed attestations. We re-fetch on every check (no caching)
    because the whole point of the alert is to catch the drain in real
    time. ``Decimal(0)`` on RPC failure — caller decides whether to alert.
    """
    contract = _strk_contract()

    async def _call() -> int:
        (raw,) = await contract.functions["balance_of"].call(int(account_address, 16))
        return int(raw)

    try:
        raw = await with_retry(
            _call, description=f"strk.balance_of({account_address})"
        )
    except (ClientError, Exception) as exc:  # noqa: BLE001
        logger.warning(f"STRK balance fetch failed for {account_address}: {exc}")
        return Decimal(0)
    return Decimal(raw) / Decimal(10**18)


# Known wrappers on mainnet Starknet — lets us render correct symbols even if
# the token contract on a given network only exposes short names (or panics on
# ``symbol()``). Keys are lowercased 0x-hex addresses.
_WELL_KNOWN: dict[str, tuple[str, int]] = {
    "0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d": ("STRK", 18),
    "0x03fe2b97c1fd336e750087d68b9b867997fd64a2661ff3ca5a7c771641e8e7ac": ("WBTC", 8),
    # NB: decimals on Starknet wrappers don't always match the wrapper's
    # token model on its origin chain. LBTC ships with 18 decimals on
    # Starknet (vs 8 on Ethereum's WBTC-style wrappers) and SolvBTC ships
    # with 8 (vs 18 elsewhere). User-reported pool amounts were off by
    # ~10^10 in either direction until we corrected this.
    "0x04daa17763b286d1e59b97c283c0b8c949994c361e426a28f743c67bdfe9a32f": ("LBTC", 18),
    "0x0593e034dda23eea82d2ba9a30960ed42cf4a01502cc2351dc9b9881f9931a68": ("tBTC", 18),
    "0x036834a40984312f7f7de8d31e3f6305b325389eaeea5b1c0664b2fb936461a4": ("SolvBTC", 8),
}


def _normalize(address_hex: str) -> str:
    a = address_hex.lower()
    if not a.startswith("0x"):
        a = "0x" + a
    # Pad to 66 chars (0x + 64 nibbles) so well-known lookups match.
    body = a[2:].lstrip("0") or "0"
    return "0x" + body.rjust(64, "0")


class TokenRegistry:
    """Async-safe cache keyed by contract address."""

    def __init__(self) -> None:
        self._cache: dict[str, TokenInfo] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, address: str | int) -> TokenInfo:
        key = _normalize(hex(address) if isinstance(address, int) else address)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

            info = await self._fetch(key)
            self._cache[key] = info
            return info

    async def prefetch(self, addresses: Iterable[str | int]) -> None:
        """Warm the cache concurrently for a batch of token addresses."""
        await asyncio.gather(*(self.get(a) for a in addresses), return_exceptions=True)

    async def _fetch(self, address_hex: str) -> TokenInfo:
        well_known = _WELL_KNOWN.get(address_hex)
        if well_known is not None:
            symbol, decimals = well_known
            return TokenInfo(address=address_hex, symbol=symbol, decimals=decimals)

        client = get_client()
        contract = Contract(address=int(address_hex, 16), abi=_ERC20_ABI, provider=client)

        async def _call_symbol() -> str | None:
            try:
                (raw,) = await contract.functions["symbol"].call()
                return _felt_to_ascii(raw)
            except (ClientError, KeyError):
                return None

        async def _call_decimals() -> int:
            try:
                (raw,) = await contract.functions["decimals"].call()
                return int(raw)
            except (ClientError, KeyError):
                return 18

        try:
            symbol, decimals = await asyncio.gather(
                with_retry(_call_symbol, description=f"symbol({address_hex})"),
                with_retry(_call_decimals, description=f"decimals({address_hex})"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"token metadata fetch failed for {address_hex}: {exc}")
            symbol, decimals = None, 18

        return TokenInfo(address=address_hex, symbol=symbol, decimals=decimals)


def _felt_to_ascii(raw: int) -> str | None:
    """Convert a felt252-encoded short-string to ASCII (best-effort)."""
    if not raw:
        return None
    try:
        b = int(raw).to_bytes((int(raw).bit_length() + 7) // 8, "big")
        text = b.decode("ascii").strip()
        return text or None
    except (OverflowError, UnicodeDecodeError):
        return None


# Module-level singleton so every consumer shares one warm cache.
token_registry = TokenRegistry()
