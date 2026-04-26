"""Telegram-HTML renderers for DTOs from :mod:`services.staking_dto`.

Only place where typed DTOs become the message strings users see. Keeping
it out of handlers means the REST API and the Telegram bot reuse the
exact same helpers (and tests live in one place).
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from typing import TYPE_CHECKING

from data.languages import translate
from services.price_service import usd_value
from services.staking_dto import (
    AttestationStatus,
    DelegatorInfo,
    DelegatorMultiPositions,
    PoolInfoDto,
    StakingSystemInfo,
    ValidatorInfo,
)

if TYPE_CHECKING:
    from services.tracking_service import TrackingEntry


DIVIDER = "─" * 24
_TABLE_WIDTH = 32  # characters inside the table — keeps lines under Telegram's wrap on mobile


# ---------------------------------------------------------------------------
# Primitive formatters
# ---------------------------------------------------------------------------

def _short(addr: str, head: int = 6, tail: int = 4) -> str:
    if len(addr) <= head + tail + 2:
        return addr
    return f"{addr[: 2 + head]}…{addr[-tail:]}"


def _fmt_amount(value: Decimal, symbol: str | None = None) -> str:
    """Format a token amount for display.

    Rules:
      - 0 → ``0 SYM``
      - |value| < 1 → up to 6 significant fractional digits, trailing zeros stripped
        (keeps small BTC amounts like 0.01210429 readable)
      - otherwise → 2 decimal places with ``,`` thousands separators (``101,219.34 STRK``)
    """
    v = float(value)
    if v == 0:
        return f"0 {symbol}" if symbol else "0"
    if abs(v) < 1:
        s = f"{v:.6f}".rstrip("0").rstrip(".")
    else:
        s = f"{v:,.2f}"
    return f"{s} {symbol}" if symbol else s


def _fmt_percent_bps(bps: int | None) -> str:
    if bps is None:
        return "—"
    return f"{bps / 100:.2f}%"


def _fmt_relative(when: datetime | None) -> str:
    if when is None:
        return "—"
    now = datetime.now(tz=timezone.utc)
    delta = when - now
    secs = int(delta.total_seconds())
    suffix = "" if secs >= 0 else " ago"
    secs = abs(secs)
    days, rem = divmod(secs, 86_400)
    hours, rem = divmod(rem, 3_600)
    mins, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if not days and mins:
        parts.append(f"{mins}m")
    if not parts:
        parts.append("now")
    return " ".join(parts) + suffix


def _code(text: str) -> str:
    return f"<code>{escape(text)}</code>"


def _fmt_utc(when: datetime | None) -> str:
    if when is None:
        return "—"
    return when.strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Attestation (inline block for validator cards)
# ---------------------------------------------------------------------------

def _attestation_badge(att: AttestationStatus | None, locale: str) -> str:
    """One-line badge; returns empty string when there's nothing interesting."""
    if att is None:
        return ""
    if att.missed_epochs == 0:
        return ""  # healthy — don't clutter the card
    return (
        f"\n⚠️ <b>{translate('attestation_header', locale)}</b>: "
        f"{translate('attestation_missed', locale, count=att.missed_epochs)}"
    )


def render_attestation(att: AttestationStatus | None, locale: str) -> str:
    """Standalone attestation block (used by legacy long-form renderer)."""
    if att is None:
        return ""
    icon = "✅" if att.is_attesting_this_epoch else "⚠️"
    healthy = att.missed_epochs == 0
    line = (
        translate("attestation_healthy", locale)
        if healthy
        else translate("attestation_missed", locale, count=att.missed_epochs)
    )
    return (
        f"\n🧾 <b>{translate('attestation_header', locale)}</b>\n"
        f"· {translate('epoch_current', locale)}: <b>{att.current_epoch}</b>\n"
        f"· {translate('last_attested_epoch', locale)}: {att.last_epoch_attested}\n"
        f"· {icon} {line}"
    )


# ---------------------------------------------------------------------------
# Entry cards — compact, one-per-message layout
# ---------------------------------------------------------------------------

def _label_or_fallback(entry: "TrackingEntry") -> str:
    if entry.label:
        return escape(entry.label)
    head = entry.address[:8]
    tail = entry.address[-4:]
    return f"{head}…{tail}"


def _visual_width(s: str) -> int:
    """Width in monospace cells, accounting for CJK and emoji.

    Python's ``len`` counts code points, but in a fixed-pitch font Hangul,
    CJK ideographs and most emoji render in 2 cells. ``ljust`` / ``rjust``
    therefore under-pad CJK strings and the column edges drift. We use
    ``unicodedata.east_asian_width`` (W/F = full-width = 2, the rest = 1)
    plus a small fallback for emoji whose category isn't FullWidth.
    """
    width = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        elif ord(ch) >= 0x1F300:  # most emoji blocks
            width += 2
        else:
            width += 1
    return width


def _pad(s: str, target: int, align: str = "left") -> str:
    """Pad ``s`` with spaces so its ``_visual_width`` matches ``target``."""
    deficit = target - _visual_width(s)
    if deficit <= 0:
        return s
    return s + " " * deficit if align == "left" else " " * deficit + s


def _strip_colon(s: str) -> str:
    """Strip both ASCII ``:`` and CJK fullwidth ``：`` so we can re-attach
    a colon ourselves without ending up with ``Pool::`` / ``池佣金：:``."""
    return s.rstrip(":：")


def _table(rows: list[tuple], width: int = _TABLE_WIDTH) -> str:
    """Render a labelled key/value list.

    Originally drew a box-art table with ``<pre>``, but Telegram's mobile
    font does NOT always render CJK glyphs as exactly two latin-cell widths
    — Hangul/汉字 visibly drift inside boxes even when the math is right.
    Plain bullets line up correctly on every locale and screen because the
    eye doesn't care about column edges when there are no edges.
    """
    if not rows:
        return ""
    body: list[str] = []
    for row in rows:
        label = _strip_colon(row[0])
        value = row[1]
        body.append(f"• <b>{escape(label)}:</b>  {value}")
    return "\n".join(body)


def _format_short_amount(value: Decimal, symbol: str | None = None) -> str:
    """Like ``_fmt_amount`` but compresses big numbers (1.2M, 950k)."""
    v = float(value)
    if v == 0:
        return f"0 {symbol}" if symbol else "0"
    abs_v = abs(v)
    if abs_v >= 1_000_000:
        s = f"{v / 1_000_000:.2f}M"
    elif abs_v >= 10_000:
        s = f"{v / 1_000:.1f}k"
    else:
        return _fmt_amount(value, symbol)
    return f"{s} {symbol}" if symbol else s


def _format_usd(value: Decimal) -> str:
    v = float(value)
    if v == 0:
        return "$0"
    if abs(v) < 0.01:
        return "<$0.01"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs(v) >= 10_000:
        return f"${v / 1_000:.1f}k"
    return f"${v:,.2f}"


def _active_pools(pools: list[PoolInfoDto]) -> list[PoolInfoDto]:
    """Return only pools with a non-zero stake (single source of truth)."""
    return [p for p in pools if p.amount_decimal > 0]


def _pool_inline(
    pools: list[PoolInfoDto], prices: dict[str, Decimal] | None
) -> str:
    """Compact inline pool list for the validator card footer.

    Format: ``STRK 2.96M ($120.5k) · WBTC 0.012104 ($936.9)``
    Only active (non-zero) pools are shown; empty pools are always omitted.
    """
    active = _active_pools(pools)
    if not active:
        return ""
    parts: list[str] = []
    for p in active:
        sym = p.token_symbol or "?"
        amount = _format_short_amount(p.amount_decimal)
        if prices:
            usd = usd_value(p.amount_decimal, sym, prices)
            if usd > 0:
                parts.append(f"{sym} {amount} ({_format_usd(usd)})")
                continue
        parts.append(f"{sym} {amount}")
    return " · ".join(parts)


def _validator_status(info: ValidatorInfo, locale: str) -> tuple[str, str]:
    """Return (icon, text) for the table's Status row.

    Status text stays short (one or two words) so the table column doesn't
    blow up — full attestation details still appear in the badge above.
    """
    if info.unstake_requested:
        return "⏳", translate("status_unstaking", locale)
    if info.attestation and info.attestation.missed_epochs > 0:
        return "⚠️", translate("status_missed", locale, count=info.attestation.missed_epochs)
    return "✅", translate("status_healthy", locale)


def _amount_with_usd(
    value: Decimal, symbol: str, prices: dict[str, Decimal] | None
) -> str:
    """e.g. ``101,219.34 STRK ≈ $4,140``. Drops the USD tail when no quote."""
    base = _fmt_amount(value, symbol)
    if not prices:
        return base
    usd = usd_value(value, symbol, prices)
    if usd == 0:
        return base
    return f"{base} ≈ {_format_usd(usd)}"


def _aggregate_stake_by_symbol(
    info: ValidatorInfo,
) -> dict[str, Decimal]:
    """Sum a validator's stake across own STRK plus every active pool.

    Mirrors the Mini App's "Total stake (own + delegations)" hero so the
    bot card and the web UI describe the same number. Empty pools are
    skipped (we don't want a "0 SolvBTC" tail on every validator).
    """
    totals: dict[str, Decimal] = {}
    if info.amount_own_strk and info.amount_own_strk > 0:
        totals["STRK"] = totals.get("STRK", Decimal(0)) + info.amount_own_strk
    for p in info.pools:
        if p.amount_decimal <= 0:
            continue
        sym = p.token_symbol or "?"
        totals[sym] = totals.get(sym, Decimal(0)) + p.amount_decimal
    return totals


def _aggregate_delegator_stake_by_symbol(
    multi: DelegatorMultiPositions,
) -> dict[str, Decimal]:
    """Same shape as :func:`_aggregate_stake_by_symbol` but for delegators.

    The delegator's "total stake" is the sum of their positions across
    every pool of the staker they delegated to (typically just one, but
    some delegators put STRK into one pool and WBTC into another under
    the same staker).
    """
    totals: dict[str, Decimal] = {}
    for p in multi.positions:
        if p.amount_decimal <= 0:
            continue
        sym = p.token_symbol or "STRK"
        totals[sym] = totals.get(sym, Decimal(0)) + p.amount_decimal
    return totals


def _total_stake_line(
    info_or_multi,
    prices: dict[str, Decimal] | None,
) -> str:
    """Render the cross-token total-stake line: ``≈ $XXX (3.06M STRK · 0.01 WBTC)``.

    Returns an empty string when there's nothing to show (no positions /
    no positive amounts). USD aggregate is dropped when no prices are
    available, leaving just the per-token breakdown.
    """
    if isinstance(info_or_multi, ValidatorInfo):
        totals = _aggregate_stake_by_symbol(info_or_multi)
    else:
        totals = _aggregate_delegator_stake_by_symbol(info_or_multi)
    if not totals:
        return ""

    breakdown = " · ".join(
        _format_short_amount(amt, sym) for sym, amt in totals.items()
    )

    if prices:
        usd_total = sum(
            (usd_value(amt, sym, prices) for sym, amt in totals.items()),
            Decimal(0),
        )
        if usd_total > 0:
            return f"≈ {_format_usd(usd_total)} ({breakdown})"
    return breakdown


def render_validator_card(
    entry: "TrackingEntry",
    locale: str,
    prices: dict[str, Decimal] | None = None,
) -> str:
    """Compact card for one tracked validator."""
    name = _label_or_fallback(entry)
    if entry.data is None:
        return (
            f"🛡 <b>{name}</b> — ⚠️ {translate('validator_not_found', locale)}\n"
            f"{_code(entry.address)}"
        )
    assert isinstance(entry.data, ValidatorInfo)
    info = entry.data

    status_icon, status_text = _validator_status(info, locale)
    header = f"🛡 <b>{name}</b> · {status_icon} {status_text}"

    # Status lives in the header next to the validator name; the current
    # epoch lives in the portfolio summary above (it's the same number for
    # everyone). Per-card table is purely the validator's own metrics.
    rows: list[tuple[str, str]] = [
        (
            translate("amount_own_2", locale).rstrip(":"),
            _amount_with_usd(info.amount_own_strk, "STRK", prices),
        ),
        (
            translate("unclaimed_rewards_own_2", locale).rstrip(":"),
            _amount_with_usd(info.unclaimed_rewards_own_strk, "STRK", prices),
        ),
        (translate("commission", locale).rstrip(":"), _fmt_percent_bps(info.commission_bps)),
    ]
    if info.unstake_requested:
        rows.append(
            (
                translate("unstake_requested", locale).rstrip(":"),
                _fmt_relative(info.unstake_time_utc),
            )
        )

    # Cross-token total stake (own + every active pool, USD-aggregated). The
    # delegator card has the same hero on the Mini App side; bringing it
    # here means /get_full_info and /get_validator_info match the visual
    # weight users see in the web UI.
    total_line = _total_stake_line(info, prices)
    if total_line:
        rows.append((translate("total_stake_label", locale).rstrip(":"), total_line))

    # Operator wallet — the address that signs attestation txs. Surfacing
    # its STRK balance here is the early-warning system for "validator ran
    # out of gas and started missing attestations". The address row holds
    # the balance; the full address is rendered as its own ``<code>`` line
    # under the table so a tap-to-copy on Telegram captures the FULL hex
    # (we used to wrap a truncated form like ``0x07b6…6d50`` in <code>,
    # which made the user copy the literal "…" character).
    operator_addr_line = ""
    if info.operational_address and info.operational_address != "0x0":
        bal_str = (
            _amount_with_usd(info.operator_strk_balance, "STRK", prices)
            if info.operator_strk_balance is not None
            else "—"
        )
        rows.append(
            (
                translate("operator_wallet_label", locale).rstrip(":"),
                bal_str,
            )
        )
        operator_addr_line = (
            f"\n    └─ {_code(info.operational_address)}"
        )

    table = _table(rows)
    attestation = _attestation_badge(info.attestation, locale)

    # Inline pool line (active pools only, no "N empty")
    pool_inline = _pool_inline(info.pools, prices)
    pools_line = (
        f"\n    └─ {_strip_colon(translate('pools_header', locale))}:  {pool_inline}"
        if pool_inline
        else ""
    )
    addr_line = f"\n       {_code(info.staker_address)}"

    return (
        header + attestation + "\n" + table
        + operator_addr_line + pools_line + addr_line
    )


def _delegator_kind_label(entry: "TrackingEntry", multi: "DelegatorMultiPositions") -> str:
    """Return the role label for the delegator card header (delegatee / bank / …)."""
    # We derive the role from the entry label if present; fall back to "delegatee".
    return entry.label or "delegatee"


def render_delegator_card(
    entry: "TrackingEntry",
    locale: str,
    prices: dict[str, Decimal] | None = None,
) -> str:
    """Compact card for one tracked ``(delegator, staker)`` pair."""
    name = _label_or_fallback(entry)
    if entry.data is None:
        return (
            f"🤝 <b>{name}</b> — ⚠️ {translate('delegator_not_found', locale)}\n"
            f"{_code(entry.address)}"
        )
    assert isinstance(entry.data, DelegatorMultiPositions)
    multi = entry.data
    if not multi.has_any:
        return (
            f"🤝 <b>{name}</b> — ⚠️ {translate('delegator_not_found', locale)}\n"
            f"{_code(multi.delegator_address)}"
        )

    is_unstaking = any(p.unpool_time_utc for p in multi.positions)
    status_icon = "⏳" if is_unstaking else "✅"
    status_text = translate(
        "status_unstaking" if is_unstaking else "status_healthy", locale
    )

    # Determine card role icon: bank-style entries track non-STRK assets primarily
    non_strk_positions = [p for p in multi.positions if (p.token_symbol or "STRK") != "STRK" and p.amount_decimal > 0]
    role_icon = "🏦" if non_strk_positions else "🤝"

    header = f"{role_icon} <b>{name}</b> · {status_icon} {status_text}"

    rewards_total = sum(
        (p.unclaimed_rewards_decimal for p in multi.positions), Decimal(0)
    )

    rows: list[tuple[str, str]] = []
    # One stake row per non-zero pool; rewards consolidated since they're all STRK.
    for p in multi.positions:
        if p.amount_decimal == 0:
            continue
        sym = p.token_symbol or "STRK"
        label = (
            translate("amount_own_2", locale).rstrip(":")
            if sym == "STRK"
            else translate("stake_token", locale, symbol=sym)
        )
        rows.append((label, _amount_with_usd(p.amount_decimal, sym, prices)))
    rows.append(
        (
            translate("unclaimed_rewards_own_2", locale).rstrip(":"),
            _amount_with_usd(rewards_total, "STRK", prices),
        )
    )
    # Commission: pools under one staker share the same commission in V2,
    # so the first non-zero value represents them all.
    commission_bps = next(
        (p.commission_bps for p in multi.positions if p.commission_bps), None
    )
    rows.append(
        (translate("pool_commission", locale).rstrip(":"), _fmt_percent_bps(commission_bps))
    )

    # Total stake for the delegator across every pool of the same staker
    # (STRK + any BTC wrappers). Same hero as the validator card / Mini App.
    total_line = _total_stake_line(multi, prices)
    if total_line:
        rows.append((translate("total_stake_label", locale).rstrip(":"), total_line))

    table = _table(rows)

    # Surface any unpool-in-progress across positions.
    unpool_blocks: list[str] = []
    for p in multi.positions:
        if p.unpool_time_utc is not None:
            sym = p.token_symbol or "STRK"
            unpool_blocks.append(
                f"\n⏳ {translate('withdrawing', locale)} ({sym}): "
                f"{_fmt_amount(p.unpool_amount_decimal, sym)} · "
                f"{_fmt_relative(p.unpool_time_utc)}"
            )

    # Bug #2 fix: self-delegation collapse.
    # If delegator_address == staker_address (self-stake), it means the validator
    # is staking into their own pool — show a single address with "(self-stake)".
    # Otherwise show the staker (validator) address the delegator points to.
    is_self_stake = multi.delegator_address == multi.staker_address
    if is_self_stake:
        addr_line = f"\n    └─ → {_code(multi.staker_address)} (self-stake)"
    else:
        addr_line = f"\n    └─ → {_code(multi.staker_address)}"

    return header + "\n" + table + "".join(unpool_blocks) + addr_line


# ---------------------------------------------------------------------------
# Legacy long-form renderers (kept for back-compat; new UI uses *_card)
# ---------------------------------------------------------------------------

def render_validator(info: ValidatorInfo, locale: str) -> str:  # noqa: D401
    """Long single-staker view. New UI prefers ``render_validator_card``."""
    from services.tracking_service import TrackingEntry

    entry = TrackingEntry(
        index=0,
        kind="validator",
        address=info.staker_address,
        pool="",
        label="",
        data=info,
    )
    return render_validator_card(entry, locale)


def render_delegator(info: DelegatorInfo, locale: str) -> str:
    """Back-compat shim. Wraps a single-pool DTO into the multi-pool one the
    new renderer expects. Kept for legacy imports; new UI uses
    ``render_delegator_card`` directly.
    """
    from services.tracking_service import TrackingEntry

    multi = DelegatorMultiPositions(
        delegator_address=info.delegator_address,
        staker_address="0x0",  # not known from a single DelegatorInfo
        positions=[info],
    )
    entry = TrackingEntry(
        index=0,
        kind="delegator",
        address=info.delegator_address,
        pool="",
        label="",
        data=multi,
    )
    return render_delegator_card(entry, locale)


# ---------------------------------------------------------------------------
# System status (/api/v1/status, optional bot /system command)
# ---------------------------------------------------------------------------

def render_system_info(info: StakingSystemInfo, locale: str = "en") -> str:
    exit_days = info.exit_wait_window_seconds // 86_400
    return (
        f"🌐 <b>Starknet Staking — {info.network}</b>\n"
        f"{DIVIDER}\n"
        f"📦 Staking contract: {_code(_short(info.staking_contract))}\n"
        f"🧾 Attestation contract: {_code(_short(info.attestation_contract))}\n"
        f"🪙 {translate('active_tokens', locale)}: <b>{len(info.active_token_addresses)}</b>\n"
        f"⏱ {translate('epoch_current', locale)}: <b>{info.current_epoch}</b>\n"
        f"💎 Min stake: <b>{_fmt_amount(info.min_stake_strk, 'STRK')}</b>\n"
        f"🚪 {translate('exit_wait_window', locale)}: <b>{exit_days} {translate('days', locale)}</b>\n"
        f"{DIVIDER}"
    )
