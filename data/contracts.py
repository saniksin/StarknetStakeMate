"""Contract registry for Starknet Staking V2 (v3.0.0).

Provides network-aware addresses and cached ABIs for the staking contract,
pool contracts, and the attestation contract. Replaces the previous hardcoded
Contract class that assumed a single contract and a dummy Account for read-only
calls (read-only calls do not need an Account at all).
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

STARKNET_RPC_URL: str | None = os.getenv("STARKNET_RPC_URL")
if not STARKNET_RPC_URL:
    raise ValueError("STARKNET_RPC_URL is not set in .env")

STARKNET_NETWORK: Network = os.getenv("STARKNET_NETWORK", "mainnet")  # type: ignore[assignment]
if STARKNET_NETWORK not in ("mainnet", "sepolia"):
    raise ValueError(f"STARKNET_NETWORK must be 'mainnet' or 'sepolia', got {STARKNET_NETWORK!r}")


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


def get_network_addresses(network: Network | None = None) -> NetworkAddresses:
    """Return the contract address bundle for the given network.

    Defaults to the env-configured network.
    """
    return _NETWORKS[network or STARKNET_NETWORK]


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
