"""Protocol-wide staking APR, read from Endur's validator index.

The Yield calculator needs the **gross** rate — the one before any
validator takes a commission — because it applies commission itself:
a validator earns the full rate on its own stake plus the commission
slice of what is delegated to it, a delegator earns the rate net of that
commission. Hand it a post-commission number and the cut gets counted
twice.

Endur publishes ``apy`` per validator already net of that validator's
commission, so the validators charging **0%** are the ones quoting the
gross figure. Measured across all 15 commission tiers on mainnet, their
own numbers reconstruct to four decimals:

    apy(validator) == gross * (1 - commission)

so reading the zero-commission rows is exact, not an approximation. When
none of them is active we fall back to un-applying the commission from a
validator that charges one, and flag the result as ``derived``.

Same failure discipline as :mod:`services.uptime_service`: always return
a DTO, never ``None``, never raise. One thing is different, though — APR
barely moves, so the *last figure we successfully read* is a far better
answer than a constant baked in at build time. Every good reading is
written to disk (``files/apr_last_good.json``, inside the data volume, so
it survives a container restart) and served back with
``status="stale"`` while the upstream is unreachable. The UI says which
of the three it is showing rather than quietly passing one off as another.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from datetime import datetime, timezone

import aiohttp

from data.all_paths import FILES_DIR
from data.contracts import DEFAULT_NETWORK, Network
from services.staking_dto import NetworkApr
from services.uptime_service import _ENDUR_BASE, _parse_dt
from utils.logger import logger

# Last known good reading per network. Lives in the data volume next to
# the SQLite DB, so a rebuilt container still starts with a real rate
# instead of a build-time constant.
_STORE_PATH = FILES_DIR / "apr_last_good.json"

# APR moves with the protocol's minting curve and total stake — slowly.
# Ten minutes is far more often than it meaningfully changes, and keeps
# the Yield tab from re-asking on every open.
_TTL_SECONDS = int(os.getenv("APR_CACHE_TTL", "600"))
_FAILURE_TTL_SECONDS = int(os.getenv("APR_FAILURE_CACHE_TTL", "60"))

_TIMEOUT = aiohttp.ClientTimeout(total=float(os.getenv("APR_TIMEOUT", "8")))

# Sorting by apy descending puts the zero-commission validators first —
# gross is the same for everyone, so the largest post-commission number
# is the one with no commission. Ten rows is enough to take a median from
# and keeps the response small (~9 KB instead of ~440 KB for the full list).
_SAMPLE_SIZE = 10

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
        "derived": apr.derived,
        "sample_size": apr.sample_size,
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
        derived=bool(row.get("derived")),
        sample_size=int(row.get("sample_size") or 0),
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


def _pick(rows: list[dict], network: Network) -> NetworkApr:
    """Choose the gross STRK / BTC rate out of a sorted validator sample."""
    active = [r for r in rows if r.get("is_active")]
    if not active:
        return _unavailable(network, "no active validators in sample")

    def _commission(row: dict) -> float | None:
        try:
            return float(row.get("commission"))
        except (TypeError, ValueError):
            return None

    zero_commission = [
        r for r in active
        if _commission(r) == 0 and _as_rate(r.get("apy")) is not None
    ]

    if zero_commission:
        strk_values = [_as_rate(r.get("apy")) for r in zero_commission]
        # ``btc_apy`` can legitimately be 0 on a network with no BTC
        # pools yet, so it is allowed through where ``apy`` is not.
        btc_values = []
        for r in zero_commission:
            try:
                btc_values.append(max(0.0, float(r.get("btc_apy") or 0)))
            except (TypeError, ValueError):
                btc_values.append(0.0)
        return NetworkApr(
            network=network,
            status="ok",
            strk_percent=statistics.median(strk_values),
            btc_percent=statistics.median(btc_values),
            derived=False,
            sample_size=len(zero_commission),
            measured_at=_parse_dt(zero_commission[0].get("updated_at")),
        )

    # Fallback: nobody is running at 0%. Un-apply the smallest commission
    # we can see. Exact by the same identity, just one division away from
    # the source, so the result is flagged.
    best: tuple[float, dict] | None = None
    for row in active:
        commission = _commission(row)
        rate = _as_rate(row.get("apy"))
        if commission is None or rate is None or commission >= 100:
            continue
        if best is None or commission < best[0]:
            best = (commission, row)
    if best is None:
        return _unavailable(network, "no validator quoted a usable apy")

    commission, row = best
    factor = 1 - commission / 100
    try:
        btc_raw = max(0.0, float(row.get("btc_apy") or 0))
    except (TypeError, ValueError):
        btc_raw = 0.0
    gross_strk = _as_rate(row.get("apy"))
    if gross_strk is None:
        return _unavailable(network, "no validator quoted a usable apy")
    return NetworkApr(
        network=network,
        status="ok",
        strk_percent=gross_strk / factor,
        btc_percent=btc_raw / factor,
        derived=True,
        sample_size=1,
        measured_at=_parse_dt(row.get("updated_at")),
    )


async def _fetch(network: Network) -> NetworkApr:
    base = _ENDUR_BASE.get(network) or ""
    if not base:
        return _unavailable(network, f"no APR source configured for {network}")
    origin = (
        "https://dashboard.endur.fi"
        if network == "mainnet"
        else "https://sepolia.dashboard.endur.fi"
    )
    url = (
        f"{base}/validators"
        f"?page=1&per_page={_SAMPLE_SIZE}&sort_by=apy&sort_order=desc"
    )
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": origin,
        "referer": origin + "/",
    }
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    logger.warning(
                        f"apr: {network} returned HTTP {response.status}"
                    )
                    return _unavailable(network, f"upstream HTTP {response.status}")
                payload = await response.json(content_type=None)
    except asyncio.TimeoutError:
        logger.warning(f"apr fetch timed out [{network}]")
        return _unavailable(network, "upstream timed out")
    except aiohttp.ClientError as exc:
        logger.warning(f"apr fetch failed [{network}]: {exc}")
        return _unavailable(network, "upstream unreachable")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"apr fetch errored [{network}]: {exc}")
        return _unavailable(network, "unexpected upstream response")

    rows = (payload or {}).get("validators") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return _unavailable(network, "upstream returned no validators")
    return _pick(rows, network)


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
