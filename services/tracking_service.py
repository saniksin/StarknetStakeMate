"""Per-user tracking: storage schema, DTO resolution and digest rendering.

Storage schema (in the ``users.tracking_data`` JSON column)::

    {
      "validators":  [{"address": "0x…", "label": "Karnot"}],
      "delegations": [{"delegator": "0x…", "staker": "0x…", "label": "My stake"}]
    }

Note on the delegation model: Staking V2 allows one validator to run
multiple token pools (STRK plus BTC wrappers). Instead of asking users for
a specific pool address, we track ``(delegator, staker)`` and enumerate the
pools at query time via ``staker_pool_info(staker)``. That way a single
tracked record covers every pool the delegator is in under that validator.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Literal

from loguru import logger

from services.formatting import (
    DIVIDER,
    _fmt_amount,
    render_delegator_card,
    render_validator_card,
)
from services.staking_dto import DelegatorInfo, DelegatorMultiPositions, ValidatorInfo
from services.staking_service import get_delegator_positions, get_validator_info

Mode = Literal["full", "reward"]


# ---------------------------------------------------------------------------
# Schema I/O helpers
# ---------------------------------------------------------------------------

def _empty() -> dict:
    return {"validators": [], "delegations": []}


def _normalize(doc: dict | None) -> dict:
    """Ensure both top-level lists exist. Legacy ``data_pair`` format is no
    longer migrated — the project decided to wipe the DB on this breaking
    change instead of resolving pool → staker via RPC at startup.
    """
    if not doc:
        return _empty()
    doc.setdefault("validators", [])
    doc.setdefault("delegations", [])
    # Drop obsolete keys silently.
    return {"validators": doc["validators"], "delegations": doc["delegations"]}


def load_tracking(tracking_data_json: str | None) -> dict:
    if not tracking_data_json:
        return _empty()
    try:
        return _normalize(json.loads(tracking_data_json))
    except json.JSONDecodeError:
        return _empty()


def dump_tracking(doc: dict) -> str:
    return json.dumps(_normalize(doc))


def total_tracked(doc: dict) -> int:
    d = _normalize(doc)
    return len(d["validators"]) + len(d["delegations"])


# ---------------------------------------------------------------------------
# Entry resolution (DTO per stored row)
# ---------------------------------------------------------------------------

@dataclass
class TrackingEntry:
    """One row from the user's tracking_data, resolved to a DTO.

    For validators, ``data`` is a :class:`ValidatorInfo`.
    For delegations, ``data`` is a :class:`DelegatorMultiPositions` (one
    DTO covering every pool of the tracked staker that the delegator is a
    member of).

    ``address`` holds the "primary" identifier — the staker address for
    validators, the delegator address for delegations. ``pool`` becomes the
    staker address for delegations (kept under this name for callback-button
    backward compat; semantic rename requires touching every call-site).
    """

    index: int
    kind: Literal["validator", "delegator"]
    address: str
    pool: str     # delegator: staker_address. validator: staking contract.
    label: str
    data: ValidatorInfo | DelegatorMultiPositions | None


async def fetch_tracking_entries(tracking_data_json: str | None) -> list[TrackingEntry]:
    doc = load_tracking(tracking_data_json)

    jobs: list[tuple[int, str, str, str, str]] = []  # (idx, kind, a1, a2, label)
    idx = 0
    for v in doc["validators"]:
        jobs.append((idx, "validator", v["address"], "", v.get("label", "")))
        idx += 1
    for d in doc["delegations"]:
        delegator = d.get("delegator") or d.get("address", "")
        staker = d.get("staker") or d.get("pool", "")
        jobs.append((idx, "delegator", delegator, staker, d.get("label", "")))
        idx += 1

    async def _one(
        i: int, kind: str, a1: str, a2: str, label: str
    ) -> TrackingEntry:
        if kind == "validator":
            info: ValidatorInfo | DelegatorMultiPositions | None = await get_validator_info(a1)
            return TrackingEntry(i, kind, a1, a2, label, info)  # type: ignore[arg-type]
        # delegator: a1 = delegator address, a2 = staker address
        multi = await get_delegator_positions(a2, a1) if a2 else None
        return TrackingEntry(i, kind, a1, a2, label, multi)  # type: ignore[arg-type]

    if not jobs:
        return []
    return await asyncio.gather(*(_one(*j) for j in jobs))


# ---------------------------------------------------------------------------
# Digest renderers (combined "send-me-everything" flows)
# ---------------------------------------------------------------------------

def _short_name(entry: TrackingEntry) -> str:
    head = entry.address[:8]
    tail = entry.address[-4:]
    prefix = "🛡" if entry.kind == "validator" else "🎱"
    return f"{prefix} {head}…{tail}"


def _entry_unclaimed_strk(entry: TrackingEntry) -> "Decimal":
    """Return the total unclaimed STRK rewards for one entry (0 if unknown)."""
    from decimal import Decimal as _D

    if entry.data is None:
        return _D(0)
    if isinstance(entry.data, ValidatorInfo):
        return entry.data.unclaimed_rewards_own_strk
    if isinstance(entry.data, DelegatorMultiPositions):
        return sum(
            (p.unclaimed_rewards_decimal for p in entry.data.positions), _D(0)
        )
    return _D(0)


def _portfolio_summary(
    entries: list[TrackingEntry],
    prices: dict[str, "Decimal"] | None,
    locale: str,
) -> str:
    """One-glance header summarizing every position together."""
    from decimal import Decimal as _D

    from data.languages import translate
    from services.formatting import (
        _amount_with_usd,
        _fmt_amount,
        _format_short_amount,
        _format_usd,
        usd_value,
    )

    from services.formatting import _active_pools

    # Stake bucketed by token. STRK from validator-own + every delegation pool.
    stake_by_token: dict[str, _D] = {}
    rewards_total = _D(0)
    # pool_count counts only ACTIVE pools (non-zero stake) — single source of truth.
    pool_count = 0
    # Current epoch is identical for every validator on the network, so we
    # surface it once at the portfolio level instead of repeating it inside
    # each card. Pick the first available; all of them will match.
    current_epoch: int | None = None
    for e in entries:
        rewards_total += _entry_unclaimed_strk(e)
        if isinstance(e.data, ValidatorInfo):
            if current_epoch is None:
                current_epoch = e.data.current_epoch
            stake_by_token["STRK"] = stake_by_token.get("STRK", _D(0)) + e.data.amount_own_strk
            pool_count += len(_active_pools(e.data.pools))
        elif isinstance(e.data, DelegatorMultiPositions):
            for pos in e.data.positions:
                if pos.amount_decimal == 0:
                    continue
                sym = pos.token_symbol or "STRK"
                stake_by_token[sym] = stake_by_token.get(sym, _D(0)) + pos.amount_decimal
                pool_count += 1

    total_usd = _D(0)
    for sym, amount in stake_by_token.items():
        if amount == 0:
            continue
        if prices:
            total_usd += usd_value(amount, sym, prices)
    if prices:
        total_usd += usd_value(rewards_total, "STRK", prices)

    total_usd_str = f"≈ {_format_usd(total_usd)}  ·  " if prices and total_usd > 0 else ""
    epoch_str = (
        f"  ·  {translate('epoch_current', locale).rstrip(':：')} {current_epoch}"
        if current_epoch is not None
        else ""
    )
    counts = translate(
        "portfolio_counts", locale,
        positions=len(entries), pools=pool_count,
    )
    return (
        f"💼 <b>{translate('portfolio_header', locale)}</b>\n"
        f"    {total_usd_str}{counts}{epoch_str}\n"
        f"{DIVIDER}"
    )


def _render_reward_entry(
    entry: TrackingEntry, prices: dict[str, "Decimal"] | None, locale: str
) -> tuple[str, "Decimal"]:
    """Return ``(line, unclaimed_strk_amount)`` for sorting/totalling."""
    from decimal import Decimal as _D

    from data.languages import translate
    from services.formatting import _amount_with_usd

    name = entry.label or _short_name(entry)
    if entry.data is None:
        key = "validator_not_found" if entry.kind == "validator" else "delegator_not_found"
        return f"• <b>{name}</b> — ⚠️ {translate(key, locale)}", _D(0)

    amount = _entry_unclaimed_strk(entry)
    rendered = _amount_with_usd(amount, "STRK", prices)
    return f"• <b>{name}</b> — 🎁 {rendered}", amount


async def render_user_tracking(
    tracking_data_json: str | None, locale: str, mode: Mode = "full"
) -> str:
    from data.languages import translate
    from services.price_service import get_usd_prices

    entries = await fetch_tracking_entries(tracking_data_json)
    if not entries:
        return translate("no_addresses_to_parse", locale)

    try:
        prices = await get_usd_prices()

        if mode == "reward":
            from decimal import Decimal as _D

            from services.formatting import _amount_with_usd

            rows: list[tuple[str, _D]] = [
                _render_reward_entry(e, prices, locale) for e in entries
            ]
            # Sort largest first; medals for the top three so it reads
            # like a leaderboard at a glance.
            rows.sort(key=lambda x: x[1], reverse=True)
            medals = ["🥇", "🥈", "🥉"]
            decorated: list[str] = []
            for i, (line, _amount) in enumerate(rows):
                prefix = medals[i] if i < len(medals) else " "
                decorated.append(line.replace("• <b>", f"{prefix} <b>", 1))

            total = sum((a for _line, a in rows), _D(0))
            total_str = _amount_with_usd(total, "STRK", prices)

            header = (
                f"🎁 <b>{translate('unclaimed_rewards_own_2', locale).rstrip(':')}</b>\n"
                f"{DIVIDER}"
            )
            footer = f"\n{DIVIDER}\n💎 <b>{translate('total_label', locale)}</b> — {total_str}"
            return header + "\n" + "\n".join(decorated) + footer

        # Full mode: portfolio summary + one card per entry + total rewards footer.
        from decimal import Decimal as _D2
        from services.formatting import _amount_with_usd as _awu

        summary = _portfolio_summary(entries, prices, locale)
        cards: list[str] = []
        for e in entries:
            if e.kind == "validator":
                cards.append(render_validator_card(e, locale, prices=prices))
            else:
                cards.append(render_delegator_card(e, locale, prices=prices))

        # Total unclaimed rewards — consolidated footer, not repeated per-card.
        rewards_total = sum((_entry_unclaimed_strk(e) for e in entries), _D2(0))
        rewards_footer = (
            f"\n{DIVIDER}\n"
            f"🎁 {translate('total_rewards_unclaimed', locale)}: "
            f"{_awu(rewards_total, 'STRK', prices)}"
        )
        return summary + "\n\n" + "\n\n".join(cards) + rewards_footer
    except Exception as exc:  # noqa: BLE001
        logger.error(f"rendering tracking digest failed: {exc}")
        return translate("error_processing_request", locale)


def render_dashboard_summary(entries: list[TrackingEntry], locale: str) -> str:
    """Compact one-line-per-position summary for /dashboard."""
    from data.languages import translate

    if not entries:
        return translate("no_addresses_to_parse", locale)

    lines: list[str] = [f"📊 <b>{translate('system_status', locale)}</b>"]
    for e in entries:
        name = e.label or _short_name(e)
        if e.data is None:
            lines.append(f"<b>{name}</b> — ⚠️")
            continue
        if e.kind == "validator":
            assert isinstance(e.data, ValidatorInfo)
            stake = _fmt_amount(e.data.amount_own_strk, "STRK")
            unclaimed = _fmt_amount(e.data.unclaimed_rewards_own_strk, "STRK")
            flag = ""
            if e.data.attestation and e.data.attestation.missed_epochs:
                flag = f"  ⚠️ {e.data.attestation.missed_epochs}m"
            lines.append(f"🛡 <b>{name}</b> — {stake} · +{unclaimed}{flag}")
        else:
            assert isinstance(e.data, DelegatorMultiPositions)
            if not e.data.has_any:
                lines.append(f"🎱 <b>{name}</b> — {translate('delegator_not_found', locale)}")
                continue
            # Summarize by token: "STRK 5000 · WBTC 0.01"
            bits: list[str] = []
            for pos in e.data.positions:
                sym = pos.token_symbol or "STRK"
                bits.append(f"{_fmt_amount(pos.amount_decimal, sym)}")
            lines.append(f"🎱 <b>{name}</b> — " + " · ".join(bits))
    return "\n".join(lines)
