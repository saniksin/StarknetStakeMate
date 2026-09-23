"""Background notifier for reward-threshold alerts.

Walks every user with a configured threshold once per hour, resolves their
tracked stakers/delegators through the service layer, and DMs a summary
when unclaimed rewards cross *any* of the user's thresholds:

  - USD-equivalent across all positions (Bug 4),
  - per-token amounts (e.g. ≥10 STRK or ≥0.001 WBTC) (Bug 4),
  - legacy ``claim_reward_msg`` (STRK-only single number).

Also optionally flags missed attestation epochs when the feature flag
``ATTESTATION_MONITOR_ENABLED`` is set.

Every configured network is walked. Off mainnet the tokens have no market,
so the USD aggregate is skipped (no prices are fetched at all) and only
token-amount thresholds can fire; those DMs carry a testnet badge and a
line saying the amounts have no monetary value.
"""
from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import aiohttp

from data.contracts import DEFAULT_NETWORK, Network, available_networks
from data.languages import translate
from data.models import get_admins, semaphore
from data.tg_bot import BOT_TOKEN
from db_api.database import (
    clear_notifications_if_empty,
    get_strk_notification_users,
)
from db_api.models import Users
from services.formatting import _fmt_amount
from services.price_service import get_usd_prices, usd_value
from services.staking_dto import DelegatorMultiPositions, ValidatorInfo
from services.tracking_service import (
    TrackingEntry,
    fetch_tracking_entries,
    load_tracking,
    total_tracked,
)
from utils.logger import logger

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
_ATTESTATION_ENABLED = os.getenv("ATTESTATION_MONITOR_ENABLED", "true").lower() == "true"


async def send_message(chat_id: int, text: str) -> None:
    url = f"{TELEGRAM_API_BASE}{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            if response.status != 200:
                logger.error(f"sendMessage failed: {await response.text()}")


def _unclaimed_by_symbol(entry: TrackingEntry) -> dict[str, Decimal]:
    """Sum unclaimed rewards by token symbol for one tracked entry."""
    if entry.data is None:
        return {}
    if isinstance(entry.data, ValidatorInfo):
        # Validator's own pool rewards are paid in STRK.
        return {"STRK": entry.data.unclaimed_rewards_own_strk}
    if isinstance(entry.data, DelegatorMultiPositions):
        return dict(entry.data.total_unclaimed_by_token)
    return {}


def _evaluate_thresholds(
    entry: TrackingEntry,
    cfg: dict,
    prices: dict[str, Decimal],
) -> list[str]:
    """Return human-readable reasons this entry crossed any threshold.

    Empty list ⇒ no alert. Each reason is a short tag like ``"$5.20 ≥ $5"``
    or ``"STRK 12.3 ≥ 10"`` so the user sees *why* the notification fired.
    """
    reasons: list[str] = []
    by_symbol = _unclaimed_by_symbol(entry)
    if not by_symbol:
        return reasons

    # USD aggregate (one threshold across all tokens of this entry).
    usd_threshold = Decimal(str(cfg.get("usd_threshold") or 0))
    if usd_threshold > 0 and prices:
        total_usd = sum(
            (usd_value(amt, sym, prices) for sym, amt in by_symbol.items()),
            Decimal(0),
        )
        if total_usd >= usd_threshold:
            reasons.append(f"${total_usd:.2f} ≥ ${usd_threshold:.2f}")

    # Per-token thresholds (independent — any one crossing fires an alert).
    token_thresholds = cfg.get("token_thresholds") or {}
    for sym, amt in by_symbol.items():
        thr = token_thresholds.get(sym)
        if thr is None:
            continue
        thr_dec = Decimal(str(thr))
        if thr_dec > 0 and amt >= thr_dec:
            reasons.append(f"{sym} {_fmt_amount(amt)} ≥ {_fmt_amount(thr_dec)}")

    return reasons


def _entry_label(entry: TrackingEntry) -> str:
    if entry.label:
        return entry.label
    head = entry.address[:8]
    tail = entry.address[-4:]
    return f"{head}…{tail}"


def _format_entry_alert(entry: TrackingEntry, locale: str) -> str:
    if entry.data is None:
        return ""
    name = _entry_label(entry)
    if isinstance(entry.data, ValidatorInfo):
        # translate('claim_for_validator') already suffixes "STRK", so pass only
        # the numeric portion via _fmt_amount with no symbol.
        amount = _fmt_amount(entry.data.unclaimed_rewards_own_strk)
        return (
            f"\n🛡 <b>{name}</b>\n"
            f"• {translate('staker_address', locale)}: <code>{entry.address}</code>\n"
            f"• {translate('claim_for_validator', locale, amount_1=amount)}"
        )
    if isinstance(entry.data, DelegatorMultiPositions):
        multi = entry.data
        lines = [
            f"\n🎱 <b>{name}</b>",
            f"• {translate('delegator_address', locale)}: <code>{multi.delegator_address}</code>",
            f"• {translate('staker_address', locale)}: <code>{multi.staker_address}</code>",
        ]
        for pos in multi.positions:
            sym = pos.token_symbol or "STRK"
            lines.append(f"• 🎁 {_fmt_amount(pos.unclaimed_rewards_decimal, sym)}")
        return "\n".join(lines)
    return ""


def _format_missed_attestation(entry: TrackingEntry, locale: str) -> str:
    """Flag validators that missed epoch attestation in the current window."""
    if not _ATTESTATION_ENABLED or not isinstance(entry.data, ValidatorInfo):
        return ""
    att = entry.data.attestation
    if att is None or att.missed_epochs == 0:
        return ""
    from services.i18n_plural import t_n
    return (
        f"\n⚠️ <b>{translate('attestation_header', locale)}</b>: "
        f"<code>{entry.address}</code> — "
        + t_n("attestation_missed", att.missed_epochs, locale, count=att.missed_epochs)
    )


def _network_badge(network: Network, locale: str) -> str:
    """Leading line marking a non-default network, or ``""`` for mainnet."""
    if network == DEFAULT_NETWORK:
        return ""
    label = translate("network_testnet_badge", locale)
    if not label or label == "network_testnet_badge":
        label = "🧪 Testnet (Sepolia)"
    return f"{label}\n"


def _no_value_note(network: Network, locale: str) -> str:
    """Reminder that testnet amounts are not money, appended to its DMs."""
    if network == DEFAULT_NETWORK:
        return ""
    note = translate("testnet_no_value_note", locale)
    if not note or note == "testnet_no_value_note":
        note = "Test tokens — their monetary value is 0."
    return f"\n<i>{note}</i>"


async def start_parse_and_send_notification(
    user: Users, prices: dict[str, Decimal], network: Network | None = None
) -> None:
    net: Network = network or DEFAULT_NETWORK
    async with semaphore:
        try:
            entries = await fetch_tracking_entries(user.tracking_data, net)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"notification fetch failed for {user.user_id} [{net}]: {exc}"
            )
            return

        cfg = user.get_notification_config(net)
        hits: list[tuple[TrackingEntry, list[str]]] = []
        for e in entries:
            reasons = _evaluate_thresholds(e, cfg, prices)
            if reasons:
                hits.append((e, reasons))

        missed = [m for m in (_format_missed_attestation(e, user.user_language) for e in entries) if m]

        if not hits and not missed:
            return

        body = _network_badge(net, user.user_language)
        body += f"{translate('strk_notification_msg', user.user_language)}\n"
        for e, reasons in hits:
            body += _format_entry_alert(e, user.user_language)
            body += f"\n• 📌 {' · '.join(reasons)}\n"
        if missed:
            body += "\n" + "\n".join(missed)
        body += _no_value_note(net, user.user_language)

        # No DB write here on purpose: this function only sends a message,
        # nothing on the user row changed. ``write_to_db`` would ``merge()``
        # every column from a snapshot that's by now several seconds stale —
        # exactly the race that overwrites concurrent UI edits (language,
        # thresholds, etc.). The watcher owns no field that needs updating
        # after a reward DM.
        await send_message(user.user_id, body)


import time

_REWARD_INTERVAL = int(os.getenv("REWARD_INTERVAL_SECONDS", "3600"))


def _sleep_until_next_boundary(interval: int) -> float:
    """Seconds until the next wall-clock interval boundary (UTC).

    For ``interval=3600`` returns the seconds-until-the-next-xx:00:00 hour
    edge. For ``interval=60`` returns the seconds-until-the-next-xx:xx:00
    minute edge. Uses ``time.time()`` (Unix epoch, always UTC) so the
    container's TZ doesn't matter.
    """
    return interval - (time.time() % interval)


async def send_strk_notification() -> None:
    """Wall-clock-aligned scheduler.

    Fires every full hour at xx:00 UTC instead of "an hour after the bot
    booted". Cycle work happens during the slot that follows each boundary.
    If the cycle overruns into the next slot, the next tick still aligns to
    the following xx:00 — we never fire back-to-back.
    """
    # First sleep aligns us to the next boundary, so subsequent ticks land
    # on round hours rather than the start-up offset.
    await asyncio.sleep(_sleep_until_next_boundary(_REWARD_INTERVAL))
    while True:
        try:
            networks = available_networks()
            users = await get_strk_notification_users()
            active: list[Users] = []
            for user in users or []:
                # "Nothing tracked" has to mean nothing on ANY network,
                # otherwise a user who keeps only testnet addresses would
                # get their config wiped and a "you track nothing" DM every
                # hour.
                if all(
                    total_tracked(load_tracking(user.tracking_data, net)) == 0
                    for net in networks
                ):
                    # Stale snapshot says "no tracked addresses" — but the
                    # user might have re-added something since. Refetch and
                    # only clear if it's *still* empty; the helper writes a
                    # targeted UPDATE so concurrent edits to other columns
                    # (language, etc.) survive.
                    locale = await clear_notifications_if_empty(user.user_id)
                    if locale is None:
                        # User added a tracked address during the cycle — leave
                        # them alone, they'll be picked up on the next pass.
                        continue
                    await send_message(
                        user.user_id,
                        translate("no_addresses_to_parse_info", locale),
                    )
                    continue
                active.append(user)

            logger.info(
                f"notifications: {len(active)} users to check "
                f"across {', '.join(networks)}"
            )
            if active:
                # One CoinGecko fetch per cycle (cached for 5 min anyway),
                # and only for mainnet — the other networks' tokens have no
                # market, so there is nothing to price and no request to make.
                prices = await get_usd_prices()
                jobs = [
                    start_parse_and_send_notification(
                        u, prices if net == DEFAULT_NETWORK else {}, net
                    )
                    for net in networks
                    for u in active
                ]
                await asyncio.gather(*jobs)
        except Exception as exc:  # noqa: BLE001
            admins = get_admins()
            logger.error(f"notification loop error: {exc!r}")
            if admins:
                await send_message(admins[0], f"Notification loop error: {exc!r}")

        await asyncio.sleep(_sleep_until_next_boundary(_REWARD_INTERVAL))
