"""Read-only Starknet staking queries, post Staking V2 (v3.0.0)."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from functools import lru_cache
from typing import Any

from loguru import logger
from starknet_py.contract import Contract
from starknet_py.net.client_errors import ClientError
from starknet_py.serialization.errors import InvalidValueException

from data.contracts import STARKNET_NETWORK, get_network_addresses, load_abi
from services.attestation_service import fetch_attestation_status
from services.rpc_client import get_client, is_domain_revert, with_retry
from services.staking_dto import (
    DelegatorInfo,
    DelegatorMultiPositions,
    PoolInfoDto,
    StakingSystemInfo,
    ValidatorInfo,
    build_unstake_datetime,
    raw_to_decimal,
)
from services.token_service import token_registry


def _unwrap_seconds(value: Any) -> int:
    """starknet-py decodes ``TimeStamp`` / ``TimeDelta`` as a dict or OrderedDict
    with a ``seconds`` key. Accept both, plus raw ints for forward compatibility.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if hasattr(value, "get"):
        return int(value.get("seconds", 0))
    return 0


# starknet-py's ``Contract(...)`` constructor parses the entire ABI eagerly
# (12+ seconds in our case for the staking contract's hand-written cairo
# interface). Cache one instance per address so we pay that cost exactly
# once per process. ``maxsize`` is generous to cover all the pools a single
# user might track without thrashing.
@lru_cache(maxsize=1)
def _staking_contract() -> Contract:
    addrs = get_network_addresses()
    return Contract(
        address=int(addrs.staking_contract, 16),
        abi=load_abi("l2_staking_contract"),
        provider=get_client(),
    )


_pool_cache: dict[str, Contract] = {}
_pool_cache_lock = asyncio.Lock()


def _build_pool_contract(address_hex: str) -> Contract:
    # ~2.3s of synchronous ABI parsing.
    return Contract(
        address=int(address_hex, 16),
        abi=load_abi("l2_pool_contract"),
        provider=get_client(),
    )


async def _pool_contract_async(address_hex: str) -> Contract:
    """Cached, off-thread pool Contract factory.

    The starknet-py constructor parses the full ABI synchronously and takes
    ~2 seconds per address. Running it on the event loop blocked every
    digest (5 pools × 2s = 10s of latency *and* every other handler stalled
    behind it). We move it to ``asyncio.to_thread`` so multiple pool builds
    can run in parallel, and we cache by address so each pool only parses
    once for the lifetime of the process.
    """
    cached = _pool_cache.get(address_hex)
    if cached is not None:
        return cached
    async with _pool_cache_lock:
        cached = _pool_cache.get(address_hex)
        if cached is not None:
            return cached
        contract = await asyncio.to_thread(_build_pool_contract, address_hex)
        _pool_cache[address_hex] = contract
        return contract


def _pool_contract(address_hex: str) -> Contract:
    """Sync accessor — only safe after the address has been warmed."""
    cached = _pool_cache.get(address_hex)
    if cached is not None:
        return cached
    contract = _build_pool_contract(address_hex)
    _pool_cache[address_hex] = contract
    return contract


def warm_pool_abi() -> None:
    """Pre-build a throwaway pool Contract so the parser hits its hot path
    before we accept user input. Doesn't help other addresses much (each
    address re-parses), but it does pay the very-first-parse tax up front.
    """
    _pool_contract("0x" + "0" * 63 + "1")


def _addr_hex(value: Any) -> str:
    """Normalize int / hex-str contract address to 0x-prefixed canonical form."""
    as_int = int(value, 16) if isinstance(value, str) else int(value)
    return "0x" + format(as_int, "064x")


def _parse_pool_info_v1(raw: dict | None) -> tuple[str, int, int] | None:
    """Return (pool_contract, amount_raw, commission_bps) from a legacy v1 pool_info.

    ``raw`` is an ``Option<StakerPoolInfoV1>`` field inside ``StakerInfoV1``.
    """
    if not isinstance(raw, dict):
        return None
    return (
        _addr_hex(raw.get("pool_contract", 0)),
        int(raw.get("amount", 0)),
        int(raw.get("commission", 0)),
    )


async def fetch_staker_raw(staker_address: str) -> dict | None:
    """Call ``get_staker_info_v1`` on the staking contract.

    Returns the decoded struct as a dict, or ``None`` if the staker does not
    exist. ``get_staker_info_v1`` returns ``Option<StakerInfoV1>``, so the
    ``None`` case is distinguishable from RPC failure.
    """
    contract = _staking_contract()

    async def _call() -> dict | None:
        try:
            (result,) = await contract.functions["get_staker_info_v1"].call(
                int(staker_address, 16)
            )
            return result
        except InvalidValueException:
            return None
        except ClientError as exc:
            # Domain reverts ("Staker does not exist") are deterministic;
            # propagating would burn 6+ seconds in the retry layer for no gain.
            if is_domain_revert(exc):
                return None
            raise exc

    try:
        return await with_retry(_call, description=f"get_staker_info_v1({staker_address})")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"fetch_staker_raw failed for {staker_address}: {exc}")
        return None


async def fetch_staker_pools_raw(staker_address: str) -> dict | None:
    """Call ``staker_pool_info`` and return the V2 multi-pool struct.

    Returns a dict with keys ``commission`` (``Option<u16>``) and ``pools``
    (list of ``{pool_contract, token_address, amount}``), or ``None`` if the
    staker does not exist.
    """
    contract = _staking_contract()

    async def _call() -> dict | None:
        try:
            (result,) = await contract.functions["staker_pool_info"].call(
                int(staker_address, 16)
            )
            return result
        except ClientError as exc:
            # "Requested entrypoint does not exist" would mean we hit an older
            # implementation — treat it like "no data". Domain reverts (e.g.
            # "Staker does not exist") are also deterministic — short-circuit
            # so the retry loop doesn't spend 6+ seconds rediscovering that.
            if "entrypoint" in str(exc).lower() or is_domain_revert(exc):
                return None
            raise exc

    try:
        return await with_retry(
            _call, description=f"staker_pool_info({staker_address})"
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"fetch_staker_pools_raw failed for {staker_address}: {exc}")
        return None


async def fetch_current_epoch() -> int:
    contract = _staking_contract()

    async def _call() -> int:
        (epoch,) = await contract.functions["get_current_epoch"].call()
        return int(epoch)

    return await with_retry(_call, description="get_current_epoch")


async def fetch_active_tokens() -> list[str]:
    """Return addresses of currently enabled staking tokens."""
    contract = _staking_contract()

    async def _call() -> list[str]:
        (tokens,) = await contract.functions["get_active_tokens"].call()
        return [_addr_hex(t) for t in tokens]

    return await with_retry(_call, description="get_active_tokens")


async def fetch_system_info() -> StakingSystemInfo:
    """Return protocol-wide parameters (min stake, exit window, epoch, tokens)."""
    addrs = get_network_addresses()
    contract = _staking_contract()

    async def _params() -> dict:
        (res,) = await contract.functions["contract_parameters_v1"].call()
        return res

    params, epoch, active_tokens = await asyncio.gather(
        with_retry(_params, description="contract_parameters_v1"),
        fetch_current_epoch(),
        fetch_active_tokens(),
    )

    min_stake = int(params.get("min_stake", 0))
    return StakingSystemInfo(
        network=STARKNET_NETWORK,
        staking_contract=addrs.staking_contract,
        attestation_contract=_addr_hex(params.get("attestation_contract", addrs.attestation_contract)),
        reward_supplier=_addr_hex(params.get("reward_supplier", 0)),
        min_stake_raw=min_stake,
        min_stake_strk=raw_to_decimal(min_stake, 18),
        exit_wait_window_seconds=_unwrap_seconds(params.get("exit_wait_window")),
        current_epoch=epoch,
        active_token_addresses=active_tokens,
    )


async def get_validator_info(
    staker_address: str,
    *,
    with_attestation: bool = True,
    with_operator_balance: bool = True,
) -> ValidatorInfo | None:
    """Aggregate the V2 validator view (info + multi-pool + attestation +
    operator wallet STRK balance)."""
    staker_raw, pools_raw, epoch = await asyncio.gather(
        fetch_staker_raw(staker_address),
        fetch_staker_pools_raw(staker_address),
        fetch_current_epoch(),
    )
    if staker_raw is None:
        return None

    legacy_pool = _parse_pool_info_v1(staker_raw.get("pool_info"))

    pools: list[PoolInfoDto] = []
    commission_bps: int | None = None

    if pools_raw and isinstance(pools_raw, dict):
        pools_list = pools_raw.get("pools") or []
        commission_opt = pools_raw.get("commission")
        if isinstance(commission_opt, int):
            commission_bps = int(commission_opt)
        for p in pools_list:
            token_hex = _addr_hex(p.get("token_address", 0))
            amount_raw = int(p.get("amount", 0))
            token_meta = await token_registry.get(token_hex)
            pools.append(
                PoolInfoDto(
                    pool_contract=_addr_hex(p.get("pool_contract", 0)),
                    token_address=token_hex,
                    token_symbol=token_meta.symbol,
                    amount_raw=amount_raw,
                    amount_decimal=raw_to_decimal(amount_raw, token_meta.decimals),
                )
            )
    elif legacy_pool is not None:
        # Fallback: pre-multi-token validators still report a single STRK pool
        # inside StakerInfoV1.pool_info. Use STRK decimals.
        pool_contract, amount_raw, commission_bps = legacy_pool
        strk = await token_registry.get(get_network_addresses().strk_token)
        pools.append(
            PoolInfoDto(
                pool_contract=pool_contract,
                token_address=strk.address,
                token_symbol=strk.symbol,
                amount_raw=amount_raw,
                amount_decimal=raw_to_decimal(amount_raw, strk.decimals),
            )
        )

    unstake_dt = build_unstake_datetime(staker_raw.get("unstake_time"))
    amount_own = int(staker_raw.get("amount_own", 0))
    unclaimed_own = int(staker_raw.get("unclaimed_rewards_own", 0))

    operational_hex = _addr_hex(staker_raw.get("operational_address", 0))

    # Attestation health + operator-wallet STRK balance run in parallel —
    # both are independent reads on independent contracts. Either failing
    # is non-fatal; we just leave the field empty.
    from services.token_service import fetch_strk_balance  # local import: avoids cycle

    async def _att() -> "AttestationStatus | None":
        if not with_attestation:
            return None
        try:
            return await fetch_attestation_status(staker_address, current_epoch=epoch)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"attestation lookup failed for {staker_address}: {exc}")
            return None

    async def _bal() -> "Decimal | None":
        if not with_operator_balance or not operational_hex or operational_hex == "0x0":
            return None
        try:
            return await fetch_strk_balance(operational_hex)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"operator balance lookup failed for {operational_hex}: {exc}")
            return None

    attestation, operator_balance = await asyncio.gather(_att(), _bal())

    return ValidatorInfo(
        staker_address=_addr_hex(staker_address),
        reward_address=_addr_hex(staker_raw.get("reward_address", 0)),
        operational_address=operational_hex,
        amount_own_raw=amount_own,
        amount_own_strk=raw_to_decimal(amount_own, 18),
        unclaimed_rewards_own_raw=unclaimed_own,
        unclaimed_rewards_own_strk=raw_to_decimal(unclaimed_own, 18),
        commission_bps=commission_bps,
        unstake_time_utc=unstake_dt,
        unstake_requested=unstake_dt is not None,
        pools=pools,
        current_epoch=epoch,
        attestation=attestation,
        operator_strk_balance=operator_balance,
    )


async def fetch_pool_member_raw(pool_address: str, member_address: str) -> dict | None:
    """Call ``get_pool_member_info_v1`` on a pool contract."""
    contract = await _pool_contract_async(pool_address)

    async def _call() -> dict | None:
        try:
            (result,) = await contract.functions["get_pool_member_info_v1"].call(
                int(member_address, 16)
            )
            return result
        except InvalidValueException:
            return None
        except ClientError as exc:
            if "contract not found" in str(exc).lower():
                return None
            raise exc

    try:
        return await with_retry(
            _call, description=f"get_pool_member_info_v1({pool_address},{member_address})"
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            f"fetch_pool_member_raw failed ({pool_address}/{member_address}): {exc}"
        )
        return None


async def fetch_pool_parameters_raw(pool_address: str) -> dict | None:
    """Call ``contract_parameters_v1`` on a pool contract (returns PoolContractInfoV1)."""
    contract = await _pool_contract_async(pool_address)

    async def _call() -> dict | None:
        (result,) = await contract.functions["contract_parameters_v1"].call()
        return result

    try:
        return await with_retry(
            _call, description=f"pool.contract_parameters_v1({pool_address})"
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"fetch_pool_parameters_raw failed ({pool_address}): {exc}")
        return None


async def get_delegator_info(
    pool_address: str, delegator_address: str
) -> DelegatorInfo | None:
    """Compose the delegator view, resolving the pool's token decimals."""
    member_raw, pool_params = await asyncio.gather(
        fetch_pool_member_raw(pool_address, delegator_address),
        fetch_pool_parameters_raw(pool_address),
    )
    if member_raw is None:
        return None

    token_hex: str | None = None
    decimals = 18  # STRK fallback
    symbol: str | None = None
    if isinstance(pool_params, dict) and pool_params.get("token_address"):
        token_hex = _addr_hex(pool_params["token_address"])
        try:
            tok = await token_registry.get(token_hex)
            decimals, symbol = tok.decimals, tok.symbol
        except Exception:  # noqa: BLE001
            pass

    amount_raw = int(member_raw.get("amount", 0))
    unclaimed_raw = int(member_raw.get("unclaimed_rewards", 0))
    unpool_amount_raw = int(member_raw.get("unpool_amount", 0))

    # In Staking V2, *delegation* (``amount``) is denominated in the pool's
    # token (STRK or a BTC wrapper), but *rewards* are always paid in STRK.
    # Dividing unclaimed rewards by an 8-decimal BTC factor gave 460B WBTC
    # for a couple of STRK reward dust.
    REWARDS_DECIMALS = 18

    return DelegatorInfo(
        delegator_address=_addr_hex(delegator_address),
        pool_contract=_addr_hex(pool_address),
        token_address=token_hex,
        token_symbol=symbol,
        reward_address=_addr_hex(member_raw.get("reward_address", 0)),
        amount_raw=amount_raw,
        amount_decimal=raw_to_decimal(amount_raw, decimals),
        unclaimed_rewards_raw=unclaimed_raw,
        unclaimed_rewards_decimal=raw_to_decimal(unclaimed_raw, REWARDS_DECIMALS),
        commission_bps=int(member_raw.get("commission", 0)),
        unpool_amount_raw=unpool_amount_raw,
        unpool_amount_decimal=raw_to_decimal(unpool_amount_raw, decimals),
        unpool_time_utc=build_unstake_datetime(member_raw.get("unpool_time")),
    )


async def get_delegator_positions(
    staker_address: str, delegator_address: str
) -> DelegatorMultiPositions:
    """Enumerate the staker's pools and return every one where the delegator
    is a member.

    This is what the bot actually needs now that a single staker can run
    multiple token pools (STRK + BTC wrappers). Asking the user for a
    specific pool address was a V1-era constraint.
    """
    pools_raw = await fetch_staker_pools_raw(staker_address)
    if not pools_raw or not isinstance(pools_raw, dict):
        return DelegatorMultiPositions(
            delegator_address=_addr_hex(delegator_address),
            staker_address=_addr_hex(staker_address),
            positions=[],
        )

    pool_contracts: list[str] = []
    for p in pools_raw.get("pools") or []:
        pool_contracts.append(_addr_hex(p.get("pool_contract", 0)))

    # Probe every pool in parallel; non-members get dropped.
    async def _probe(pool_addr: str) -> DelegatorInfo | None:
        return await get_delegator_info(pool_addr, delegator_address)

    results = await asyncio.gather(*(_probe(p) for p in pool_contracts))
    positions = [r for r in results if r is not None]

    return DelegatorMultiPositions(
        delegator_address=_addr_hex(delegator_address),
        staker_address=_addr_hex(staker_address),
        positions=positions,
    )
