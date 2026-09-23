"""Contract registry for Starknet Staking V2 (v3.0.0).

Provides network-aware addresses and cached ABIs for the staking contract,
pool contracts, and the attestation contract. Replaces the previous hardcoded
Contract class that assumed a single contract and a dummy Account for read-only
calls (read-only calls do not need an Account at all).

Two networks can be served **at the same time** since v3.1: mainnet is the
always-on default (the bot only ever talks to it), and Sepolia is an
optional second target that the Mini App can switch to. Everything below
is therefore keyed by network rather than baked into module constants.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv

from data.all_paths import ABI_DIR
from utils.read_json import read_json

load_dotenv()

Network = Literal["mainnet", "sepolia"]

NETWORKS: tuple[Network, ...] = ("mainnet", "sepolia")

# Friendly aliases the Mini App / query strings may use. ``testnet`` is what
# the UI calls it; ``sepolia`` is what the protocol calls it.
_NETWORK_ALIASES: dict[str, Network] = {
    "mainnet": "mainnet",
    "main": "mainnet",
    "sn_main": "mainnet",
    "sepolia": "sepolia",
    "testnet": "sepolia",
    "test": "sepolia",
    "sn_sepolia": "sepolia",
}


class UnknownNetworkError(ValueError):
    """Raised for a network name we don't serve (bad input or not configured)."""


STARKNET_RPC_URL: str | None = os.getenv("STARKNET_RPC_URL")
if not STARKNET_RPC_URL:
    raise ValueError("STARKNET_RPC_URL is not set in .env")

STARKNET_NETWORK: Network = os.getenv("STARKNET_NETWORK", "mainnet")  # type: ignore[assignment]
if STARKNET_NETWORK not in NETWORKS:
    raise ValueError(f"STARKNET_NETWORK must be 'mainnet' or 'sepolia', got {STARKNET_NETWORK!r}")

# The network every legacy call-site (and the whole Telegram bot) means when
# it doesn't say. Kept as a separate name so intent reads clearly at call
# sites: ``STARKNET_NETWORK`` is "what .env configured", ``DEFAULT_NETWORK``
# is "what you get when you pass None".
DEFAULT_NETWORK: Network = STARKNET_NETWORK

# Second, optional endpoint. When unset the testnet tab simply never appears
# in the Mini App and every ``network=sepolia`` request returns 503 — no
# half-working state where the UI offers a tab that can't load.
_TESTNET_RPC_URL: str | None = (
    os.getenv("STARKNET_TESTNET_RPC_URL")
    or os.getenv("STARKNET_SEPOLIA_RPC_URL")
    or None
)

_RPC_URLS: dict[Network, str] = {DEFAULT_NETWORK: STARKNET_RPC_URL}
if _TESTNET_RPC_URL:
    # Two guards, both against the same class of mistake — serving one
    # chain's data under the other's label:
    #   * both variables pointing at the same node;
    #   * STARKNET_NETWORK already set to sepolia, which would make this
    #     variable overwrite the primary endpoint.
    if (
        DEFAULT_NETWORK != "sepolia"
        and _TESTNET_RPC_URL.strip() != STARKNET_RPC_URL.strip()
    ):
        _RPC_URLS["sepolia"] = _TESTNET_RPC_URL.strip()


@dataclass(frozen=True)
class NetworkAddresses:
    """Well-known staking-system addresses per network.

    `attestation_contract` is discoverable at runtime via
    `staking.contract_parameters_v1().attestation_contract` but is pinned here
    for fast start-up; the services layer re-verifies it on first use.
    """

    staking_contract: str
    attestation_contract: str  # derived from contract_parameters_v1
    strk_token: str            # STRK ERC-20
    chain_id_hex: str          # as returned by starknet_chainId


MAINNET = NetworkAddresses(
    staking_contract="0x00ca1702e64c81d9a07b86bd2c540188d92a2c73cf5cc0e508d949015e7e84a7",
    attestation_contract="0x010398fe631af9ab2311840432d507bf7ef4b959ae967f1507928f5afe888a99",
    strk_token="0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d",
    chain_id_hex="0x534e5f4d41494e",  # "SN_MAIN"
)

SEPOLIA = NetworkAddresses(
    # Addresses per Starkware public deployment; re-check on first run.
    staking_contract="0x03745ab04a431fc02871a139be6b93d9260b0ff3e779ad9c8b377183b23109f1",
    attestation_contract="0x03f32e152b9637c31bfcf73e434f78591067a01ba070505ff6ee195642c9acfb",
    strk_token="0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d",
    chain_id_hex="0x534e5f5345504f4c4941",  # "SN_SEPOLIA"
)

_NETWORKS: dict[Network, NetworkAddresses] = {"mainnet": MAINNET, "sepolia": SEPOLIA}


def resolve_network(value: str | None) -> Network:
    """Normalize a user-supplied network name.

    ``None`` / empty → :data:`DEFAULT_NETWORK`. Accepts the UI's
    ``testnet`` spelling as an alias for ``sepolia`` so the query string
    can stay human-readable without the backend growing a second
    vocabulary. Raises :class:`UnknownNetworkError` on anything else —
    silently falling back to mainnet would show mainnet numbers under a
    testnet heading.
    """
    if value is None:
        return DEFAULT_NETWORK
    key = str(value).strip().lower()
    if not key:
        return DEFAULT_NETWORK
    try:
        return _NETWORK_ALIASES[key]
    except KeyError as exc:
        raise UnknownNetworkError(f"unknown network: {value!r}") from exc


def get_rpc_url(network: Network | None = None) -> str:
    """RPC endpoint for the given network.

    Raises :class:`UnknownNetworkError` when the network is valid but no
    endpoint was configured for it (the usual case: no
    ``STARKNET_TESTNET_RPC_URL`` in .env).
    """
    net = network or DEFAULT_NETWORK
    try:
        return _RPC_URLS[net]
    except KeyError as exc:
        raise UnknownNetworkError(
            f"no RPC endpoint configured for {net!r}; "
            f"set STARKNET_TESTNET_RPC_URL in .env"
        ) from exc


def is_network_available(network: Network | None = None) -> bool:
    """True when we have an RPC endpoint for this network."""
    return (network or DEFAULT_NETWORK) in _RPC_URLS


def available_networks() -> list[Network]:
    """Configured networks, default first.

    The Mini App reads this through ``GET /api/v1/networks`` to decide
    whether to draw the mainnet/testnet switch at all.
    """
    ordered = [DEFAULT_NETWORK] + [n for n in NETWORKS if n != DEFAULT_NETWORK]
    return [n for n in ordered if n in _RPC_URLS]


def get_network_addresses(network: Network | None = None) -> NetworkAddresses:
    """Return the contract address bundle for the given network.

    Defaults to the env-configured network.
    """
    return _NETWORKS[network or DEFAULT_NETWORK]


@lru_cache(maxsize=None)
def load_abi(name: str) -> list:
    """Read and cache an ABI JSON file from `smart_contracts_abi/`.

    `name` is the file stem, e.g. ``"l2_staking_contract"``.
    """
    return read_json(ABI_DIR / f"{name}.json")


# ---------------------------------------------------------------------------
# Backwards-compatibility shim for legacy imports.
# Older modules (pre-refactor) imported `Contracts.L2_STAKING_CONTRACT` as a
# simple (address, abi) container. Re-export the same shape, now sourced from
# the network bundle + load_abi().
# ---------------------------------------------------------------------------

class _ContractRef:
    """Lightweight back-compat wrapper. Prefer `services.staking_service`."""

    def __init__(self, address_hex: str, abi: list) -> None:
        self.hex_address = address_hex
        self.address = int(address_hex, 16)
        self.abi = abi


class Contracts:
    """Legacy registry kept for backward compatibility.

    New code should use :mod:`services.staking_service` instead of reaching
    for this directly.
    """

    _addrs = get_network_addresses()

    L2_STAKING_CONTRACT = _ContractRef(
        address_hex=_addrs.staking_contract,
        abi=load_abi("l2_staking_contract"),
    )
    L2_ATTESTATION_CONTRACT = _ContractRef(
        address_hex=_addrs.attestation_contract,
        abi=load_abi("l2_attestation_contract"),
    )
