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
from starknet_py.net.client_errors import ClientError

from data.contracts import DEFAULT_NETWORK, Network, get_network_addresses
from services.rpc_client import get_client, with_retry
from services.staking_dto import TokenInfo

_TTL = int(os.getenv("TOKEN_CACHE_TTL", "3600"))


# STRK token. Hard-coded because operator-balance lookups need it constantly
# and we want to avoid a DB / config detour. The address happens to be the
# same on mainnet and Sepolia (STRK is a predeployed system token), but we
# still route through ``get_network_addresses`` so a future divergence is a
# one-line change rather than a hunt through this module.
STRK_TOKEN_ADDRESS = "0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d"


def strk_token_address(network: Network | None = None) -> str:
    return get_network_addresses(network or DEFAULT_NETWORK).strk_token


# Starknet-keccak selectors, hard-coded so we don't pay for the hash on
# every call. Everything in this module talks to tokens through
# ``client.call_contract`` with these rather than through a starknet-py
# ``Contract``: the minimal hand-written ABI we used to carry stopped
# parsing under starknet-py 0.30 (the Cairo-1 parser wants ``impl``
# entries, and silently produced a Contract with zero functions), which
# made every ``symbol()`` / ``decimals()`` lookup fail closed. Only the
# hard-coded ``_WELL_KNOWN`` table hid it on mainnet. Raw calls have no
# ABI to drift.
_BALANCE_OF_SELECTOR = 0x35a73cd311a05d46deda634c5ee045db92f811b4e74bca4437fcb5302b7af33
_SYMBOL_SELECTOR = 0x216B05C387BAB9AC31918A3E61672F4618601F3C598A2F3F2710F37053E1EA4
_DECIMALS_SELECTOR = 0x4C4FB1AB068F6039D5780C68DD0FA2F8742CCEB3426D19667778CA7F3518A9


async def fetch_strk_balance(
    account_address: str, *, network: Network | None = None
) -> Decimal:
    """Return ``account``'s on-chain STRK balance, scaled to whole tokens.

    Used for the operator-wallet low-balance alert: validators must keep
    a small STRK reserve to pay attestation gas, and running dry causes
    silent missed attestations. We re-fetch on every check (no caching)
    because the whole point of the alert is to catch the drain in real
    time. ``Decimal(0)`` on RPC failure — caller decides whether to alert.

    We bypass ``Contract.functions["balance_of"].call(...)`` and assemble
    the ``u256`` manually from the two returned felts (low/high). The
    minimal hand-written ERC-20 ABI lacks a struct definition for ``u256``
    and earlier versions of starknet-py silently returned ``0`` instead of
    raising on the type mismatch — masking real balances as "wallet empty".
    """
    from starknet_py.net.client_models import Call  # local: cheap import

    net: Network = network or DEFAULT_NETWORK
    client = get_client(net)
    call = Call(
        to_addr=int(strk_token_address(net), 16),
        selector=_BALANCE_OF_SELECTOR,
        calldata=[int(account_address, 16)],
    )

    async def _call() -> int:
        result = await client.call_contract(call=call, block_hash="latest")
        # u256 = (low: u128, high: u128), little-endian as a 2-felt tuple.
        if not result or len(result) < 2:
            return 0
        low, high = int(result[0]), int(result[1])
        return (high << 128) | low

    try:
        raw = await with_retry(
            _call, description=f"strk.balance_of({account_address})"
        )
    except Exception as exc:  # noqa: BLE001
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
    # strkBTC — Starknet's BTC LST. ERC-20 underlying for the new
    # delegation-pool token added 2026-05-08; pool contract is
    # 0x0136cb830054c3eebcf3c82951f3cd9f846aeff841328660c5777bf298f033f5.
    "0x0787150e306e6eae6e3f79dea881770e8bbff2c1b8eb490f969669ee945b3135": ("strkBTC", 8),
}


def _normalize(address_hex: str) -> str:
    a = address_hex.lower()
    if not a.startswith("0x"):
        a = "0x" + a
    # Pad to 66 chars (0x + 64 nibbles) so well-known lookups match.
    body = a[2:].lstrip("0") or "0"
    return "0x" + body.rjust(64, "0")


class TokenRegistry:
    """Async-safe cache keyed by ``(network, contract address)``.

    The network is part of the key because an address is only unique
    *within* a chain: a Sepolia test wrapper can collide with a mainnet
    token and would otherwise inherit its symbol and — worse — its
    decimals, silently scaling every amount by 10^10.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], TokenInfo] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def get(
        self, address: str | int, *, network: Network | None = None
    ) -> TokenInfo:
        net: Network = network or DEFAULT_NETWORK
        key = (net, _normalize(hex(address) if isinstance(address, int) else address))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

            info = await self._fetch(key[1], net)
            self._cache[key] = info
            return info

    async def prefetch(
        self, addresses: Iterable[str | int], *, network: Network | None = None
    ) -> None:
        """Warm the cache concurrently for a batch of token addresses."""
        await asyncio.gather(
            *(self.get(a, network=network) for a in addresses),
            return_exceptions=True,
        )

    async def _fetch(self, address_hex: str, network: Network) -> TokenInfo:
        # ``_WELL_KNOWN`` is a mainnet table. On other networks the same
        # address means something else (or nothing), so we only trust it
        # for the token that genuinely shares an address across both —
        # STRK — and go on-chain for everything else.
        well_known = _WELL_KNOWN.get(address_hex)
        if well_known is not None and (
            network == "mainnet" or address_hex == _normalize(strk_token_address(network))
        ):
            symbol, decimals = well_known
            return TokenInfo(address=address_hex, symbol=symbol, decimals=decimals)

        client = get_client(network)

        async def _call_symbol() -> str | None:
            try:
                result = await _token_call(client, address_hex, _SYMBOL_SELECTOR)
            except (ClientError, asyncio.TimeoutError):
                return None
            return _decode_symbol(result)

        async def _call_decimals() -> int:
            try:
                result = await _token_call(client, address_hex, _DECIMALS_SELECTOR)
            except (ClientError, asyncio.TimeoutError):
                return 18
            if not result:
                return 18
            try:
                value = int(result[0])
            except (TypeError, ValueError):
                return 18
            # A token claiming 0 or >36 decimals is broken or hostile;
            # believing it would scale a balance into nonsense. Fall back
            # to the ERC-20 default instead.
            return value if 0 < value <= 36 else 18

        try:
            symbol, decimals = await asyncio.gather(
                with_retry(_call_symbol, description=f"symbol({address_hex})"),
                with_retry(_call_decimals, description=f"decimals({address_hex})"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"token metadata fetch failed for {address_hex}: {exc}")
            symbol, decimals = None, 18

        if symbol is None:
            logger.warning(f"token {address_hex} did not return a usable symbol")

        return TokenInfo(address=address_hex, symbol=symbol, decimals=decimals)


async def _token_call(client, address_hex: str, selector: int) -> list[int]:
    """One no-argument view call against a token, at the latest block."""
    from starknet_py.net.client_models import Call  # local: cheap import

    call = Call(to_addr=int(address_hex, 16), selector=selector, calldata=[])
    return list(await client.call_contract(call=call, block_hash="latest"))


def _decode_symbol(result: list[int]) -> str | None:
    """Decode ``symbol()`` from either shape a Starknet token may return.

    Older tokens answer with a single ``felt252`` short string. Tokens
    built on the modern OpenZeppelin Cairo components answer with a
    ``ByteArray``: ``[full_word_count, *full_words, pending_word,
    pending_word_len]``. Both appear among the staking tokens, so we
    sniff the shape by length rather than guessing per network.
    """
    if not result:
        return None
    if len(result) == 1:
        return _felt_to_ascii(result[0])
    try:
        full_words = int(result[0])
        words = result[1 : 1 + full_words]
        pending_word = int(result[1 + full_words])
        pending_len = int(result[2 + full_words])
    except (IndexError, TypeError, ValueError):
        return None
    chunks: list[bytes] = []
    for word in words:
        # Every full word carries exactly 31 bytes.
        chunks.append(int(word).to_bytes(31, "big"))
    if pending_len:
        try:
            chunks.append(int(pending_word).to_bytes(pending_len, "big"))
        except OverflowError:
            return None
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        return None
    return _printable(text)


def _printable(text: str) -> str | None:
    """Return ``text`` if it looks like a symbol a human would recognise.

    A felt that happens to hold small integers decodes into control
    characters; treating that as a symbol puts a tofu box in the pool
    row. Anything not fully printable is reported as "unknown" instead,
    which the renderer already handles.
    """
    text = text.strip()
    if not text or not text.isprintable():
        return None
    return text


def _felt_to_ascii(raw: int) -> str | None:
    """Convert a felt252-encoded short-string to ASCII (best-effort)."""
    if not raw:
        return None
    try:
        b = int(raw).to_bytes((int(raw).bit_length() + 7) // 8, "big")
        return _printable(b.decode("ascii"))
    except (OverflowError, UnicodeDecodeError):
        return None


# Module-level singleton so every consumer shares one warm cache.
token_registry = TokenRegistry()
