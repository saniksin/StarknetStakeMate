"""Validator uptime, sourced from Endur's staking indexer.

The staking contracts can tell us whether a validator attested in the
current epoch and which epoch it last won — enough for "missed N epochs",
not enough for a percentage. Endur indexes the attestation events and
publishes the aggregate as ``liveliness`` (0–100) on a public, CORS-open
API with no key. That is what we read here.

Why not Voyager: its ``/api/staking/validator-details`` sits behind
Cloudflare's bot challenge and needs a ``cf_clearance`` cookie bound to a
specific browser session (IP + TLS fingerprint + user agent). Replaying
one from a server returns the interstitial, and it expires within hours
regardless — a dependency that breaks on its own schedule is worse than
no dependency. Endur covers the same field.

Everything here fails soft, but never *silently*: a failure comes back as
a :class:`ValidatorUptime` with ``status="unavailable"`` rather than
``None``, so the card renders "couldn't fetch this" instead of quietly
dropping the block. A feature that vanishes when someone else changes
their API is a feature nobody notices is broken.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime

import aiohttp

from data.contracts import DEFAULT_NETWORK, Network
from services.staking_dto import ValidatorUptime
from utils.logger import logger

# Per-network base URLs. Overridable so a deployment can point at a mirror
# (or switch them off entirely by setting an empty value).
_ENDUR_BASE: dict[str, str] = {
    "mainnet": os.getenv(
        "ENDUR_API_URL_MAINNET", "https://api.dashboard.endur.fi/api/query"
    ).strip(),
    "sepolia": os.getenv(
        "ENDUR_API_URL_SEPOLIA", "https://staking-api-sepolia.endur.fi/api/query"
    ).strip(),
}

# Endur refreshes a validator's record roughly every ten minutes, and not
# uniformly across validators. Caching for five minutes keeps a dashboard
# that polls every few seconds from hammering someone else's API without
# ever showing data the upstream would have replaced.
_TTL_SECONDS = int(os.getenv("UPTIME_CACHE_TTL", "300"))

# Their API answers a request for an address it doesn't index with HTTP
# 500 and this message — a "not found", despite the status. Retrying it
# just burns time, so we short-circuit on the body rather than the code.
_NOT_FOUND_MARKER = "validator not found"

_TIMEOUT = aiohttp.ClientTimeout(total=float(os.getenv("UPTIME_TIMEOUT", "8")))

# A failed fetch is retried much sooner than a successful one is
# refreshed: the card should recover as soon as the upstream does.
_FAILURE_TTL_SECONDS = int(os.getenv("UPTIME_FAILURE_CACHE_TTL", "30"))

_cache: dict[tuple[str, str], tuple[ValidatorUptime, float]] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def normalize_validator_address(address: str) -> str | None:
    """Return the 66-character lowercase form Endur's API insists on.

    A Starknet address is a felt, so ``0x1``, ``0x01`` and sixty-two
    leading zeros followed by ``1`` are all the same address and users
    paste whichever their tooling printed. Endur's per-validator route
    accepts exactly one spelling — anything shorter, longer or uppercase
    comes back as HTTP 500 — so every lookup goes through here first.

    ``None`` when the input isn't a hex address at all.
    """
    if not address:
        return None
    try:
        value = int(str(address).strip(), 16)
    except (TypeError, ValueError):
        return None
    if value < 0 or value.bit_length() > 252:
        return None
    return "0x" + format(value, "064x")


def _parse_dt(raw: object) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        # Their timestamps are ISO-8601 with a trailing ``Z``, which
        # ``fromisoformat`` only learned to parse in 3.11.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _unavailable(address: str, detail: str) -> ValidatorUptime:
    return ValidatorUptime(address=address, status="unavailable", detail=detail)


def _parse(address: str, payload: dict) -> ValidatorUptime:
    raw = payload.get("liveliness")
    if raw is None:
        # Upstream answered but dropped the one field we came for — a
        # schema change, not an outage. Worth saying out loud.
        return _unavailable(address, "no liveliness field in response")
    try:
        percent = float(raw)
    except (TypeError, ValueError):
        return _unavailable(address, f"liveliness is not a number: {raw!r}")
    # Clamp rather than reject: a value outside 0–100 means their
    # aggregation changed shape, and a pinned 100 is a better failure
    # than a card that refuses to render.
    percent = min(100.0, max(0.0, percent))
    logo = payload.get("logo")
    return ValidatorUptime(
        address=address,
        status="ok",
        percent=percent,
        is_active=bool(payload.get("is_active")),
        is_unstaking=bool(payload.get("is_unstaking")),
        name=(payload.get("name") or None),
        logo_url=logo if isinstance(logo, str) and logo.startswith("https://") else None,
        active_since=_parse_dt(payload.get("active_since")),
        measured_at=_parse_dt(payload.get("updated_at")),
    )


async def _fetch(address: str, network: Network) -> ValidatorUptime:
    base = _ENDUR_BASE.get(network) or ""
    if not base:
        return _unavailable(address, f"no uptime source configured for {network}")
    url = f"{base}/validators/{address}"
    # Their API is CORS-scoped to the dashboard origin; send the matching
    # Referer so we look like the client they expect rather than an
    # anonymous scraper.
    origin = (
        "https://dashboard.endur.fi"
        if network == "mainnet"
        else "https://sepolia.dashboard.endur.fi"
    )
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": origin,
        "referer": origin + "/",
    }
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url, headers=headers) as response:
                text = await response.text()
                if response.status != 200:
                    if _NOT_FOUND_MARKER in text.lower():
                        # Not indexed (yet). A perfectly normal answer for
                        # a validator that just registered — and note it
                        # arrives as HTTP 500, not 404.
                        return ValidatorUptime(address=address, status="not_indexed")
                    logger.warning(
                        f"uptime: {network} returned {response.status} for {address}"
                    )
                    return _unavailable(address, f"upstream HTTP {response.status}")
                payload = await response.json(content_type=None)
    except asyncio.TimeoutError:
        logger.warning(f"uptime fetch timed out for {address} [{network}]")
        return _unavailable(address, "upstream timed out")
    except aiohttp.ClientError as exc:
        logger.warning(f"uptime fetch failed for {address} [{network}]: {exc}")
        return _unavailable(address, "upstream unreachable")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"uptime fetch errored for {address} [{network}]: {exc}")
        return _unavailable(address, "unexpected upstream response")

    if not isinstance(payload, dict):
        return _unavailable(address, "upstream returned a non-object body")
    return _parse(address, payload)


async def fetch_validator_uptime(
    address: str, *, network: Network | None = None
) -> ValidatorUptime:
    """Return the validator's uptime. Always an object, never ``None``.

    Failures come back as ``status="unavailable"`` with a short
    ``detail`` so the card can say *what* went wrong instead of silently
    omitting the block — see :class:`ValidatorUptime`. Never raises.

    Cached for :data:`_TTL_SECONDS` per ``(network, address)``; concurrent
    callers for the same key share one request.
    """
    net: Network = network or DEFAULT_NETWORK
    normalized = normalize_validator_address(address)
    if normalized is None:
        return _unavailable(str(address or ""), "not a Starknet address")

    key = (net, normalized)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and now < cached[1]:
        return cached[0]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Another coroutine may have filled the cache while we waited.
        cached = _cache.get(key)
        now = time.monotonic()
        if cached is not None and now < cached[1]:
            return cached[0]

        result = await _fetch(normalized, net)
        # "Not indexed" is cached for the full TTL — a validator Endur
        # doesn't know won't appear within five minutes, and re-asking on
        # every render would add latency for nothing. A genuine failure
        # gets a much shorter TTL so a brief outage doesn't freeze the
        # card into an error for five minutes after it recovers.
        ttl = _TTL_SECONDS if result.status != "unavailable" else _FAILURE_TTL_SECONDS
        _cache[key] = (result, now + ttl)
        return result


def invalidate_uptime_cache() -> None:
    """Drop every cached entry. Test hook."""
    _cache.clear()
    _locks.clear()
