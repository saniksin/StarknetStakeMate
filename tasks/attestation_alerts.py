"""Attestation watcher — fast-cycle missed-epoch alerts.

Separate from the hourly reward notifier in :mod:`tasks.strk_notification`
because attestation latency matters: missing two epochs in a row eats
real money, and the user wants to know within a minute, not within an
hour.

Per-user opt-in via ``notification_config["attestation_alerts"]`` — off by
default after a fresh validator add. State is kept in
``notification_config["_attestation_state"]`` so we don't re-spam the same
"missed 3 epochs" message every cycle.
"""
from __future__ import annotations

import asyncio
import os

import aiohttp

from data.languages import translate
from data.models import semaphore
from data.tg_bot import BOT_TOKEN
from db_api.database import (
    db,
    get_strk_notification_users,
    update_attestation_state,
    update_operator_balance_was_below,
)
from db_api.models import Users
from services.attestation_service import fetch_attestation_status
from services.staking_service import fetch_current_epoch, fetch_staker_raw
from services.token_service import fetch_strk_balance
from services.tracking_service import load_tracking
from utils.logger import logger


def _addr_hex_norm(value) -> str:
    """Normalize a contract-decoded int / hex string to ``0x``-prefixed lower-case.

    We avoid importing the staking_service private ``_addr_hex`` so the
    alert task stays decoupled from internal helpers.
    """
    if value is None:
        return ""
    if isinstance(value, int):
        return "0x" + format(value, "x")
    s = str(value).lower()
    return s if s.startswith("0x") else "0x" + s

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
_INTERVAL = int(os.getenv("ATTESTATION_INTERVAL_SECONDS", "60"))


async def _send(chat_id: int, text: str) -> None:
    url = f"{TELEGRAM_API_BASE}{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            if response.status != 200:
                logger.error(f"sendMessage failed: {await response.text()}")


def _validator_label(staker_address: str, validators: list[dict]) -> str:
    """Return the user-facing label for a tracked staker, falling back to a
    short address when no label was set."""
    for v in validators:
        if (v.get("address") or "").lower() == staker_address.lower():
            label = v.get("label") or ""
            if label:
                return label
            head, tail = staker_address[:8], staker_address[-4:]
            return f"{head}…{tail}"
    head, tail = staker_address[:8], staker_address[-4:]
    return f"{head}…{tail}"


def _resolve_subscribed_set(cfg: dict, validators: list[dict]) -> set[str]:
    """Return the lower-cased staker addresses the user wants alerts for.

    Supports both the new per-validator schema (``attestation_alerts_for``)
    and the legacy global boolean (``attestation_alerts``) so older configs
    keep working without a migration job.
    """
    raw = cfg.get("attestation_alerts_for")
    if isinstance(raw, list):
        return {str(a).lower() for a in raw if a}
    if cfg.get("attestation_alerts"):
        return {(v.get("address") or "").lower() for v in validators}
    return set()


async def _check_user(
    user: Users, current_epoch: int, *, epoch_changed: bool
) -> tuple[dict | None, dict | None]:
    """Run one attestation + operator-balance check for a single user.

    Returns ``(att_state, bal_state)``. Each is the new dict to persist
    when something changed, or ``None`` when no DB write is needed.
    Does NOT mutate the passed ``user`` object — the caller does atomic,
    targeted updates so we never clobber concurrent user-driven edits
    (language, tracking_data, etc.).

    Operator-balance alerts fire **once per epoch boundary** (``epoch_changed``
    True). On every other tick we skip the balance RPC entirely — the user
    only wants one DM per epoch, and we already know "did the boundary
    just tick" from the in-memory ``_last_seen_epoch`` in ``_run_cycle``.
    The attestation watcher itself stays continuous: missed epochs need a
    sub-minute alert SLA.
    """
    cfg = user.get_notification_config()
    doc = load_tracking(user.tracking_data)
    validators = doc.get("validators", [])
    if not validators:
        return None, None

    locale = user.user_language or "en"
    subscribed_att = _resolve_subscribed_set(cfg, validators)
    balance_min = float(cfg.get("operator_balance_min_strk") or 0)

    if not subscribed_att and balance_min <= 0:
        return None, None

    att_state = dict(cfg.get("_attestation_state") or {})
    was_below_state: dict[str, bool] = {
        str(k): True
        for k, v in (cfg.get("_operator_balance_was_below") or {}).items()
        if v
    }
    att_changed = False
    bal_changed = False

    # Balance section runs only on the first tick after an epoch flip.
    # Skip the staker_raw + balance_of RPCs for everyone else — pure
    # bandwidth saving, no behavioural impact (we'd rediscover the same
    # answer next boundary anyway).
    do_balance = epoch_changed and balance_min > 0

    for v in validators:
        staker = (v.get("address") or "").lower()
        if not staker:
            continue
        label = _validator_label(staker, validators)

        # ---- Attestation health (continuous, every tick) ---------------
        if staker in subscribed_att:
            try:
                status = await fetch_attestation_status(
                    staker, current_epoch=current_epoch
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"attestation fetch failed for {staker}: {exc}")
                status = None
            if status is not None:
                new_missed = status.missed_epochs
                old_missed = int(att_state.get(staker, 0))
                if new_missed > old_missed:
                    from services.i18n_plural import t_n
                    await _send(
                        user.user_id,
                        t_n(
                            "attestation_alert_missed", new_missed, locale,
                            label=label, count=new_missed,
                            epoch=status.current_epoch,
                        ),
                    )
                    att_state[staker] = new_missed
                    att_changed = True
                elif new_missed == 0 and old_missed > 0:
                    await _send(
                        user.user_id,
                        translate("attestation_alert_recovered", locale, label=label),
                    )
                    att_state.pop(staker, None)
                    att_changed = True
                elif new_missed < old_missed and new_missed > 0:
                    att_state[staker] = new_missed
                    att_changed = True

        # ---- Operator wallet STRK balance (epoch-boundary only) --------
        # Skip when the boundary hasn't ticked, the feature is disabled,
        # or the user didn't opt this validator into attestation alerts
        # (the balance alert is part of the same per-validator channel).
        if not do_balance or staker not in subscribed_att:
            continue
        try:
            staker_raw = await fetch_staker_raw(staker)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"staker_raw fetch failed for {staker}: {exc}")
            continue
        if not staker_raw:
            continue
        op_addr = _addr_hex_norm(staker_raw.get("operational_address", 0))
        if not op_addr or op_addr == "0x0":
            continue
        try:
            balance = await fetch_strk_balance(op_addr)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"operator balance fetch failed for {op_addr}: {exc}")
            continue
        balance_f = float(balance)
        was_below = was_below_state.get(staker, False)
        is_below = balance_f < balance_min

        # Per-epoch state machine. The cells:
        #   was=False, is=False → silent, no state change
        #   was=False, is=True  → first time below this epoch → low-balance alert
        #   was=True,  is=True  → still below this epoch     → low-balance alert (again)
        #   was=True,  is=False → recovered since last boundary → recovered alert
        # Either alert fires **once** per epoch boundary because we only
        # enter this branch when ``epoch_changed=True``.
        if is_below:
            await _send(
                user.user_id,
                translate(
                    "operator_low_balance_alert", locale,
                    label=label, balance=balance_f, threshold=balance_min,
                    epoch=current_epoch,
                ),
            )
            if not was_below:
                was_below_state[staker] = True
                bal_changed = True
        elif was_below:
            await _send(
                user.user_id,
                translate(
                    "operator_balance_recovered", locale,
                    label=label, balance=balance_f, threshold=balance_min,
                    epoch=current_epoch,
                ),
            )
            was_below_state.pop(staker, None)
            bal_changed = True
        # Final case (was=False, is=False) = silent, no state change.

    return (att_state if att_changed else None,
            was_below_state if bal_changed else None)


# In-memory cursor for "did the epoch number change since last cycle?".
# Reset to 0 on process start so the first cycle after a restart always
# counts as an epoch flip — that one wasted check is cheap, the
# alternative (stashing the cursor in the DB) couples a notifier-only
# concern to schema changes.
_last_seen_epoch: int = 0


def _reset_last_seen_epoch_for_tests() -> None:
    """Test hook: clear the module-level epoch cursor between scenarios."""
    global _last_seen_epoch
    _last_seen_epoch = 0


async def _run_cycle() -> None:
    global _last_seen_epoch

    users = await get_strk_notification_users()
    # Keep ``get_strk_notification_users`` as the broad-net query; filter
    # out non-subscribers here so the SQL stays simple. Subscription is
    # the union of attestation alerts (legacy bool or per-validator list)
    # and the new operator-balance alert.
    def _has_subscription(u: Users) -> bool:
        cfg = u.get_notification_config()
        if cfg.get("attestation_alerts"):
            return True
        if cfg.get("attestation_alerts_for"):
            return True
        if float(cfg.get("operator_balance_min_strk") or 0) > 0:
            return True
        return False

    candidates = [u for u in (users or []) if _has_subscription(u)]
    if not candidates:
        return

    # One RPC for the whole cycle — current_epoch is the same for everyone.
    try:
        current_epoch = await fetch_current_epoch()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"attestation cycle: current_epoch fetch failed: {exc}")
        return

    epoch_changed = current_epoch != _last_seen_epoch
    _last_seen_epoch = current_epoch
    if epoch_changed:
        logger.info(f"epoch boundary tick: now {current_epoch}")

    async def _process(u: Users) -> None:
        async with semaphore:
            try:
                att_state, bal_state = await _check_user(
                    u, current_epoch, epoch_changed=epoch_changed
                )
                if att_state is not None:
                    await update_attestation_state(u.user_id, att_state)
                if bal_state is not None:
                    await update_operator_balance_was_below(u.user_id, bal_state)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"attestation_alerts({u.user_id}) failed: {exc}")

    await asyncio.gather(*(_process(u) for u in candidates))


import time as _time


def _sleep_until_next_boundary(interval: int) -> float:
    """See ``tasks.strk_notification._sleep_until_next_boundary``."""
    return interval - (_time.time() % interval)


async def send_attestation_alerts() -> None:
    """Wall-clock-aligned watcher — fires at xx:xx:00 every minute (UTC)."""
    logger.info(f"attestation watcher started (interval={_INTERVAL}s)")
    await asyncio.sleep(_sleep_until_next_boundary(_INTERVAL))
    while True:
        try:
            await _run_cycle()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"attestation watcher cycle error: {exc!r}")

        await asyncio.sleep(_sleep_until_next_boundary(_INTERVAL))
