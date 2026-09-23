"""Protocol-wide staking APR, computed from the staking contracts.

The Yield calculator needs the **gross** rate — the one before any
validator takes a commission — because it applies commission itself: a
validator earns the full rate on its own stake plus the commission slice
of what is delegated to it, a delegator earns the rate net of that
commission. Hand it a post-commission number and the cut gets counted
twice.

Nothing third-party is involved. APR is just emission over stake, and
both sides are on chain:

    rewards_per_epoch = reward_supplier.calculate_current_epoch_rewards()
    epochs_per_year   = year / staking.get_epoch_info().epoch_duration
    APR_strk          = rewards.strk * epochs_per_year / staking.get_total_stake()

Checked against Endur's published mainnet figure: 7.4825% both ways, to
four decimals. (On Sepolia the two diverge — 82.7% on chain against their
44.0% — which is a point in favour of reading the chain rather than an
index of it.)

BTC pools are the one place a price is unavoidable. The protocol pays
*their* rewards in STRK too, sized against BTC collateral, so turning
that into a percentage means comparing two different assets:

    APR_btc = rewards.btc * epochs_per_year * price(STRK)
              / (btc_staking_power * price(BTC))

Without prices the BTC figure is simply absent; the STRK one never is.

Every successful reading is persisted to ``files/apr_last_good.json``
(inside the data volume, so it survives a container rebuild) and served
back with ``status="stale"`` if a later read fails. APR barely moves, so
yesterday's real number beats a constant baked in at build time — as long
as the UI says which of the two it is showing.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone

from data.all_paths import FILES_DIR
from data.contracts import DEFAULT_NETWORK, Network, get_network_addresses
from services.staking_dto import NetworkApr
from services.uptime_service import _parse_dt
from utils.logger import logger

_SECONDS_PER_YEAR = 365 * 24 * 3600

# Starknet-keccak selectors, hard-coded so nothing has to parse an ABI we
# don't ship. ``calculate_current_epoch_rewards`` lives on the reward
# supplier, the other two on the staking contract.
_EPOCH_REWARDS_SELECTOR = 0x28E40F9CA652DD3D88E10F80E876DC8363C85B4E2D08D96F8AAAB3A44F52887
_TOTAL_STAKE_SELECTOR = 0x226FFC5DB8F68325947F4C4FCBEA7117624ED26D4A1354693F63DE203C453C8
_TOTAL_STAKING_POWER_SELECTOR = 0x30E15D92E5EEFA441D334E87E43851F89FD69413DF3711C1739E883A1E66EA8

# Last known good reading per network. Lives in the data volume next to
# the SQLite DB, so a rebuilt container still starts with a real rate
# instead of a build-time constant.
_STORE_PATH = FILES_DIR / "apr_last_good.json"

# APR moves with the protocol's minting curve and total stake — slowly.
# Ten minutes is far more often than it meaningfully changes, and keeps
# the Yield tab from re-asking on every open.
_TTL_SECONDS = int(os.getenv("APR_CACHE_TTL", "600"))
_FAILURE_TTL_SECONDS = int(os.getenv("APR_FAILURE_CACHE_TTL", "60"))

_cache: dict[str, tuple[NetworkApr, float]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _load_store() -> dict:
    """Read the persisted readings. ``{}`` on any problem — this is a
    cache, and a corrupt one must never take the endpoint down."""
    try:
        raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"apr store unreadable ({_STORE_PATH}): {exc}")
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_reading(network: Network, apr: NetworkApr) -> None:
    """Persist one good reading, merging with whatever else is stored.

    Written to a temp file and renamed so a crash mid-write can't leave a
    truncated JSON behind — the next read would then throw away a
    perfectly good figure for the other network too.
    """
    store = _load_store()
    store[network] = {
        "strk_percent": apr.strk_percent,
        "btc_percent": apr.btc_percent,
        "measured_at": apr.measured_at.isoformat() if apr.measured_at else None,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = _STORE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
        tmp.replace(_STORE_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"could not persist APR reading: {exc}")


def _last_good(network: Network, detail: str) -> NetworkApr:
    """The most recent successful reading, or ``unavailable`` if none.

    ``measured_at`` deliberately keeps the ORIGINAL timestamp: the whole
    point of the stale state is that the UI can show how old the number
    really is.
    """
    row = _load_store().get(network)
    if not isinstance(row, dict):
        return _unavailable(network, detail)
    strk = _as_rate(row.get("strk_percent"))
    if strk is None:
        return _unavailable(network, detail)
    try:
        btc = max(0.0, float(row.get("btc_percent") or 0))
    except (TypeError, ValueError):
        btc = 0.0
    measured = _parse_dt(row.get("measured_at")) or _parse_dt(row.get("saved_at"))
    return NetworkApr(
        network=network,
        status="stale",
        strk_percent=strk,
        btc_percent=btc,
        measured_at=measured,
        detail=detail,
    )


def _unavailable(network: Network, detail: str) -> NetworkApr:
    return NetworkApr(network=network, status="unavailable", detail=detail)


def _as_rate(raw: object) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # A rate outside this band is a unit change or a bug upstream, not a
    # staking yield. Better to fall back than to prefill the calculator
    # with something absurd.
    if not (0 < value <= 1000):
        return None
    return value


def _u256(felts: list[int], index: int) -> int | None:
    """These getters return plain felts, not u256 pairs — but guard the
    index anyway so a signature change is a ``None``, not an IndexError."""
    try:
        return int(felts[index])
    except (IndexError, TypeError, ValueError):
        return None


async def _call(client, address: str, selector: int) -> list[int]:
    from starknet_py.net.client_models import Call  # local: cheap import

    call = Call(to_addr=int(address, 16), selector=selector, calldata=[])
    return list(await client.call_contract(call=call, block_hash="latest"))


async def _fetch(network: Network) -> NetworkApr:
    """Read emission and stake off the chain and divide.

    Everything here runs against the same node the rest of the app uses,
    so there is no separate outage mode: if this fails, the validator
    cards are broken too.
    """
    from services.rpc_client import get_client
    from services.staking_service import _staking_contract, fetch_epoch_info

    addrs = get_network_addresses(network)
    client = get_client(network)
    contract = _staking_contract(network)

    try:
        async def _params() -> dict:
            (res,) = await contract.functions["contract_parameters_v1"].call()
            return res

        params, epoch_info, total_raw, power_raw = await asyncio.gather(
            _params(),
            fetch_epoch_info(network=network),
            _call(client, addrs.staking_contract, _TOTAL_STAKE_SELECTOR),
            _call(client, addrs.staking_contract, _TOTAL_STAKING_POWER_SELECTOR),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"apr: staking reads failed [{network}]: {exc}")
        return _unavailable(network, "staking contract unreachable")

    duration = int((epoch_info or {}).get("epoch_duration") or 0)
    if duration <= 0:
        return _unavailable(network, "epoch duration unavailable")
    epochs_per_year = _SECONDS_PER_YEAR / duration

    total_staked = _u256(total_raw, 0)
    if not total_staked:
        return _unavailable(network, "total stake unavailable")

    reward_supplier = params.get("reward_supplier")
    if not reward_supplier:
        return _unavailable(network, "reward supplier address unavailable")
    supplier_hex = "0x" + format(int(reward_supplier), "064x")

    try:
        rewards_raw = await _call(client, supplier_hex, _EPOCH_REWARDS_SELECTOR)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"apr: epoch rewards read failed [{network}]: {exc}")
        return _unavailable(network, "reward supplier unreachable")

    # (rewards for STRK pools, rewards for BTC pools) — both denominated
    # in STRK, split by the protocol's ``alpha``.
    strk_rewards = _u256(rewards_raw, 0)
    btc_rewards = _u256(rewards_raw, 1)
    if strk_rewards is None:
        return _unavailable(network, "epoch rewards unavailable")

    strk_percent = _as_rate(
        strk_rewards * epochs_per_year / total_staked * 100
    )
    if strk_percent is None:
        return _unavailable(network, "computed STRK APR out of range")

    return NetworkApr(
        network=network,
        status="ok",
        strk_percent=strk_percent,
        btc_percent=await _btc_percent(
            network=network,
            btc_rewards=btc_rewards,
            epochs_per_year=epochs_per_year,
            btc_power=_u256(power_raw, 1),
        ),
        measured_at=datetime.now(timezone.utc),
    )


async def _btc_percent(
    *,
    network: Network,
    btc_rewards: int | None,
    epochs_per_year: float,
    btc_power: int | None,
) -> float | None:
    """BTC-pool APR, or ``None`` when it cannot honestly be stated.

    The protocol pays BTC stakers in STRK, sized against BTC collateral,
    so the percentage is a ratio between two assets and needs both
    prices. ``None`` (rather than zero) when either is missing: the UI
    then keeps its previous BTC figure instead of claiming the pools
    yield nothing.
    """
    if not btc_rewards or not btc_power:
        return None
    from decimal import Decimal

    from services.price_service import get_usd_prices

    try:
        prices = await get_usd_prices()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"apr: price fetch failed [{network}]: {exc}")
        return None
    strk_price = prices.get("STRK")
    btc_price = prices.get("WBTC") or prices.get("BTC")
    if not strk_price or not btc_price:
        return None

    # ``btc_power`` is the protocol's BTC stake normalised to 18 decimals,
    # the same scale the STRK amounts use.
    rewards_usd = (
        Decimal(btc_rewards) / Decimal(10**18)
        * Decimal(str(epochs_per_year))
        * Decimal(str(strk_price))
    )
    collateral_usd = Decimal(btc_power) / Decimal(10**18) * Decimal(str(btc_price))
    if collateral_usd <= 0:
        return None
    return _as_rate(float(rewards_usd / collateral_usd * 100))


async def fetch_network_apr(network: Network | None = None) -> NetworkApr:
    """Return the gross staking APR for ``network``. Never raises.

    Cached for :data:`_TTL_SECONDS`; failures for a much shorter window so
    the Yield tab picks up a recovered upstream on the next open.
    """
    net: Network = network or DEFAULT_NETWORK
    now = time.monotonic()
    cached = _cache.get(net)
    if cached is not None and now < cached[1]:
        return cached[0]

    lock = _locks.setdefault(net, asyncio.Lock())
    async with lock:
        cached = _cache.get(net)
        now = time.monotonic()
        if cached is not None and now < cached[1]:
            return cached[0]

        result = await _fetch(net)
        if result.status == "ok":
            # Remember it: next time the upstream is down this becomes the
            # answer instead of a build-time constant.
            _save_reading(net, result)
            ttl = _TTL_SECONDS
        else:
            result = _last_good(net, result.detail or "upstream unavailable")
            ttl = _FAILURE_TTL_SECONDS
        _cache[net] = (result, now + ttl)
        return result


def invalidate_apr_cache() -> None:
    """Drop every cached entry. Test hook."""
    _cache.clear()
    _locks.clear()
