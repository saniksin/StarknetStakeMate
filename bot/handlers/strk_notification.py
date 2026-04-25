"""Notification thresholds — USD aggregate and per-token (Bug 4).

Stored on ``Users.notification_config`` as
``{"usd_threshold": float, "token_thresholds": {symbol: float}}``.

The legacy ``Users.claim_reward_msg`` is still respected by the worker but
new writes go to the JSON config so the UI can model both modes uniformly.
"""
from __future__ import annotations

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from bot.handlers.clear_state import finish_operation
from data.languages import translate
from db_api.database import Users, write_to_db
from services.price_service import (
    ThresholdParseError,
    ThresholdParseErrorCode,
    parse_token_threshold,
    reward_symbols,
)
from services.tracking_service import load_tracking, total_tracked


class RewardClaimState(StatesGroup):
    waiting_for_threshold = State()         # legacy STRK-only
    waiting_for_usd = State()               # Bug 4: USD aggregate
    waiting_for_token = State()             # Bug 4: per-token "SYM AMOUNT"


def _short_addr(addr: str) -> str:
    return f"{addr[:8]}…{addr[-4:]}"


def _validator_label(v: dict) -> str:
    return v.get("label") or _short_addr(v.get("address", ""))


def _attestation_alerts_for(cfg: dict) -> set[str]:
    """Read the per-validator opt-in set, with a tiny migration from the old
    boolean flag to keep existing users' settings intact.

    Old schema had ``attestation_alerts: bool`` (all-or-nothing). New schema
    is ``attestation_alerts_for: list[str]`` of lower-cased staker addresses.
    The bool, if present, is treated as "everything currently tracked".
    """
    raw = cfg.get("attestation_alerts_for")
    if isinstance(raw, list):
        return {str(a).lower() for a in raw if a}
    if cfg.get("attestation_alerts"):
        return {"*"}  # sentinel: legacy "all on" — resolved at use-site
    return set()


def _attestation_summary_label(cfg: dict, validators: list[dict], locale: str) -> str:
    """Caption for the parent-menu button — shows ``2/3`` so the user sees
    the toggle state at a glance without opening the submenu."""
    enabled = _attestation_alerts_for(cfg)
    total = len(validators)
    if "*" in enabled:
        on = total
    else:
        on = sum(1 for v in validators if (v.get("address") or "").lower() in enabled)
    return f"{translate('attestation_toggle', locale)}: {on}/{total}"


def create_strk_notification_menu(
    locale: str, summary_label: str | None = None
) -> ReplyKeyboardMarkup:
    """STRK reward submenu — purely USD/token thresholds. Attestation alerts
    live as a sibling under the parent ``open_notification_menu``.
    The ``summary_label`` arg is accepted for back-compat but ignored.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("set_usd_threshold", locale))],
            [KeyboardButton(text=translate("set_token_threshold", locale))],
            [KeyboardButton(text=translate("show_strk_reward_notification", locale))],
            [KeyboardButton(text=translate("disable_strk_reward_notification", locale))],
            [KeyboardButton(text=translate("cancel", locale))],
        ],
        resize_keyboard=True,
    )


def _attestation_submenu(
    locale: str, validators: list[dict], enabled: set[str]
) -> ReplyKeyboardMarkup:
    """One row per tracked validator with its current ✅/⬜ state, plus
    bulk-action and back rows."""
    rows: list[list[KeyboardButton]] = []
    for v in validators:
        addr = (v.get("address") or "").lower()
        on = ("*" in enabled) or (addr in enabled)
        marker = "✅" if on else "⬜"
        rows.append([KeyboardButton(text=f"{marker} {_validator_label(v)}")])
    rows.append([
        KeyboardButton(text=translate("attestation_enable_all", locale)),
        KeyboardButton(text=translate("attestation_disable_all", locale)),
    ])
    rows.append([KeyboardButton(text=translate("back", locale))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def open_strk_notification_menu(
    message: types.Message, state: FSMContext, user_locale: str
):
    notification_menu_kb = create_strk_notification_menu(user_locale)
    await message.reply(
        text=translate("strk_notification", locale=user_locale),
        reply_markup=notification_menu_kb,
        parse_mode="HTML",
    )


class AttestationMenuState(StatesGroup):
    """User is browsing the per-validator attestation submenu."""

    picking = State()


async def open_attestation_submenu(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
):
    """Entry point: user tapped the ``🛡 Attestation alerts`` row in the
    parent menu. Show one row per tracked validator with its current state.
    """
    doc = load_tracking(user_object.tracking_data)
    validators = doc.get("validators", [])
    if not validators:
        await message.reply(
            translate("no_addresses_to_parse", user_locale), parse_mode="HTML"
        )
        return
    cfg = user_object.get_notification_config()
    enabled = _attestation_alerts_for(cfg)

    # Map button caption → staker address so the followup handler can flip
    # the right validator without re-parsing labels.
    picker = {
        f"{'✅' if ('*' in enabled or (v.get('address') or '').lower() in enabled) else '⬜'} "
        f"{_validator_label(v)}": (v.get("address") or "").lower()
        for v in validators
    }
    await state.update_data(att_picker=picker)
    await state.set_state(AttestationMenuState.picking)

    kb = _attestation_submenu(user_locale, validators, enabled)
    await message.reply(
        translate("attestation_submenu_prompt", user_locale),
        reply_markup=kb,
        parse_mode="HTML",
    )


async def handle_attestation_submenu(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
):
    """Process taps inside the per-validator submenu.

    Recognised inputs:
      - "Back"            → close submenu, reopen parent.
      - "Enable all"      → opt every tracked validator in.
      - "Disable all"     → opt every tracked validator out.
      - "<marker> <label>"→ flip that single validator.
    """
    text = (message.text or "").strip()
    doc = load_tracking(user_object.tracking_data)
    validators = doc.get("validators", [])
    cfg = user_object.get_notification_config()
    current = _attestation_alerts_for(cfg)

    if text == translate("back", user_locale):
        await state.clear()
        # Return to the parent (notifications) menu — attestation lives
        # there as a sibling of "STRK reward notifications", not nested
        # under it.
        from bot.handlers.notification import open_notification_menu
        await open_notification_menu(message, state, user_locale, user_object)
        return

    if text == translate("attestation_enable_all", user_locale):
        new_set = {(v.get("address") or "").lower() for v in validators}
        await _persist_attestation(user_object, cfg, new_set, current)
        await message.reply(
            translate("attestation_alerts_enabled", user_locale), parse_mode="HTML"
        )
        await open_attestation_submenu(message, state, user_locale, user_object)
        return

    if text == translate("attestation_disable_all", user_locale):
        await _persist_attestation(user_object, cfg, set(), current)
        await message.reply(
            translate("attestation_alerts_disabled", user_locale), parse_mode="HTML"
        )
        await open_attestation_submenu(message, state, user_locale, user_object)
        return

    # Per-validator toggle — look up the address from the cached picker map.
    data = await state.get_data()
    picker: dict[str, str] = data.get("att_picker", {})
    target = picker.get(text)
    if not target:
        # Unknown text — re-show submenu so the user can retry.
        await open_attestation_submenu(message, state, user_locale, user_object)
        return

    # Resolve the legacy "*" sentinel into a concrete set on first edit so
    # individual toggles work after the user touched anything.
    if "*" in current:
        current = {(v.get("address") or "").lower() for v in validators}

    if target in current:
        current.discard(target)
    else:
        current.add(target)
    await _persist_attestation(user_object, cfg, current, current)
    await open_attestation_submenu(message, state, user_locale, user_object)


async def _persist_attestation(
    user_object: Users,
    cfg: dict,
    new_set: set[str],
    old_set: set[str],
) -> None:
    """Write ``attestation_alerts_for`` and reset state for newly-disabled
    stakers so a re-enable later sees the missed-epoch counter as fresh."""
    cfg["attestation_alerts_for"] = sorted(new_set)
    cfg.pop("attestation_alerts", None)  # drop the legacy bool
    state = dict(cfg.get("_attestation_state") or {})
    # Trim cached state for stakers no longer subscribed.
    for staker in list(state.keys()):
        if staker.lower() not in new_set:
            state.pop(staker, None)
    cfg["_attestation_state"] = state
    user_object.set_notification_config(cfg)
    await write_to_db(user_object)


# Kept for back-compat with handlers/__init__ exports — old callers tapped
# a single global toggle. New flow goes through ``open_attestation_submenu``.
toggle_attestation_alerts = open_attestation_submenu


# ---------------------------------------------------------------------------
# Capacity gate — used by every "set …" entry point
# ---------------------------------------------------------------------------

def _has_tracked(user_object: Users) -> bool:
    return total_tracked(load_tracking(user_object.tracking_data)) > 0


def _cancel_kb(user_locale: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=translate("cancel", locale=user_locale))]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ---------------------------------------------------------------------------
# Legacy STRK-only flow — kept as a private fallback path. It now writes into
# ``notification_config.token_thresholds["STRK"]`` so a single source of truth
# governs alerts.
# ---------------------------------------------------------------------------

async def start_set_threshold(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
):
    if not _has_tracked(user_object):
        await message.reply(
            translate("no_addresses_to_parse", user_locale), parse_mode="HTML"
        )
        return
    await message.reply(
        translate("enter_claim_threshold", locale=user_locale),
        parse_mode="HTML",
        reply_markup=_cancel_kb(user_locale),
    )
    await state.set_state(RewardClaimState.waiting_for_threshold)


async def set_claim_threshold(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
):
    if message.text == translate("cancel", locale=user_locale):
        await finish_operation(message, state, user_locale)
        return
    try:
        threshold = float((message.text or "").strip())
        if threshold < 0:
            raise ValueError
    except ValueError:
        await message.reply(
            translate("invalid_threshold", locale=user_locale), parse_mode="HTML"
        )
        return

    cfg = user_object.get_notification_config()
    cfg["token_thresholds"]["STRK"] = threshold
    # Legacy entry-point (the bare "set_strk_reward_notification" button).
    # Same single-mode rule as the new setters: arming a STRK threshold
    # clears the USD aggregate so users don't end up double-armed.
    if threshold > 0:
        cfg["usd_threshold"] = 0.0
    user_object.set_notification_config(cfg)
    user_object.claim_reward_msg = 0  # migrate away from the legacy column
    await write_to_db(user_object)

    await finish_operation(
        message, state, user_locale,
        privious_msg=translate("threshold_set_success", locale=user_locale).format(threshold),
        cancel_msg=False,
    )


# ---------------------------------------------------------------------------
# USD aggregate threshold
# ---------------------------------------------------------------------------

async def start_set_usd_threshold(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
):
    if not _has_tracked(user_object):
        await message.reply(
            translate("no_addresses_to_parse", user_locale), parse_mode="HTML"
        )
        return
    await message.reply(
        translate("enter_usd_threshold", locale=user_locale),
        parse_mode="HTML",
        reply_markup=_cancel_kb(user_locale),
    )
    await state.set_state(RewardClaimState.waiting_for_usd)


async def set_usd_threshold(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
):
    if message.text == translate("cancel", locale=user_locale):
        await finish_operation(message, state, user_locale)
        return
    raw = (message.text or "").strip().lstrip("$").replace(",", "")
    try:
        amount = float(raw)
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.reply(
            translate("invalid_threshold", locale=user_locale), parse_mode="HTML"
        )
        return

    cfg = user_object.get_notification_config()
    cfg["usd_threshold"] = amount
    # Single-mode rule: arming a positive USD threshold clears any STRK
    # token threshold (and vice versa in ``set_token_threshold``). A user
    # should never end up notified twice for the same reward stream from
    # two competing rules. Setting 0 only disables USD without touching
    # the other mode — that lets the user explicitly turn off USD while
    # keeping a previously-armed STRK threshold.
    if amount > 0:
        cfg["token_thresholds"] = {}
    user_object.set_notification_config(cfg)
    user_object.claim_reward_msg = 0
    await write_to_db(user_object)

    await finish_operation(
        message, state, user_locale,
        privious_msg=translate("usd_threshold_set_success", locale=user_locale).format(amount=amount),
        cancel_msg=False,
    )


# ---------------------------------------------------------------------------
# Per-token threshold ("SYM AMOUNT" or "SYM 0" to clear)
# ---------------------------------------------------------------------------

async def start_set_token_threshold(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
):
    if not _has_tracked(user_object):
        await message.reply(
            translate("no_addresses_to_parse", user_locale), parse_mode="HTML"
        )
        return
    # Reward-eligible symbols only (STRK). BTC wrappers can be staked but
    # rewards are always paid in STRK, so a per-token threshold for them
    # would never trigger.
    symbols_hint = ", ".join(reward_symbols())
    prompt = translate("enter_token_threshold", locale=user_locale).format(
        symbols=symbols_hint
    )
    await message.reply(prompt, parse_mode="HTML", reply_markup=_cancel_kb(user_locale))
    await state.set_state(RewardClaimState.waiting_for_token)


# Map parser-error codes → locale keys. Each error gets a precise message
# so users see *what* went wrong instead of the generic "введите число".
_PARSE_ERROR_MSG: dict[ThresholdParseErrorCode, str] = {
    ThresholdParseErrorCode.EMPTY: "threshold_err_empty",
    ThresholdParseErrorCode.MISSING_SYMBOL: "threshold_err_missing_symbol",
    ThresholdParseErrorCode.MISSING_AMOUNT: "threshold_err_missing_amount",
    ThresholdParseErrorCode.UNKNOWN_SYMBOL: "threshold_err_unknown_symbol",
    ThresholdParseErrorCode.BAD_NUMBER: "threshold_err_bad_number",
    ThresholdParseErrorCode.NEGATIVE: "invalid_threshold",
    ThresholdParseErrorCode.TOO_MANY_TOKENS: "threshold_err_too_many",
}


async def set_token_threshold(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
):
    if message.text == translate("cancel", locale=user_locale):
        await finish_operation(message, state, user_locale)
        return

    try:
        canonical_sym, amount_dec = parse_token_threshold(
            message.text or "", reward_symbols()
        )
    except ThresholdParseError as exc:
        key = _PARSE_ERROR_MSG.get(exc.code, "invalid_input")
        # UNKNOWN_SYMBOL message embeds the typed symbol + valid list.
        if exc.code == ThresholdParseErrorCode.UNKNOWN_SYMBOL:
            text = translate(key, locale=user_locale).format(
                symbol=exc.detail, allowed=", ".join(reward_symbols())
            )
        else:
            text = translate(key, locale=user_locale)
        await message.reply(text, parse_mode="HTML")
        return

    amount = float(amount_dec)

    cfg = user_object.get_notification_config()
    if amount == 0:
        cfg["token_thresholds"].pop(canonical_sym, None)
    else:
        cfg["token_thresholds"][canonical_sym] = amount
        # Single-mode rule: arming a positive token threshold clears the
        # USD aggregate (mirror of ``set_usd_threshold``). See the comment
        # there for why we don't symmetrically clear when amount == 0.
        cfg["usd_threshold"] = 0.0
    user_object.set_notification_config(cfg)
    user_object.claim_reward_msg = 0
    await write_to_db(user_object)

    await finish_operation(
        message, state, user_locale,
        privious_msg=translate("token_threshold_set_success", locale=user_locale).format(
            symbol=canonical_sym, amount=amount
        ),
        cancel_msg=False,
    )


# ---------------------------------------------------------------------------
# Disable / show
# ---------------------------------------------------------------------------

async def clear_claim_threshold(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
):
    cfg = user_object.get_notification_config()
    has_any = (
        cfg.get("usd_threshold", 0) > 0
        or any(v > 0 for v in (cfg.get("token_thresholds") or {}).values())
        or (user_object.claim_reward_msg or 0) > 0
    )
    if not has_any:
        await message.reply(
            translate("claim_threshold_is_zero", locale=user_locale), parse_mode="HTML"
        )
        return

    user_object.set_notification_config({})
    user_object.claim_reward_msg = 0
    await write_to_db(user_object)

    await finish_operation(
        message, state, user_locale,
        privious_msg=translate("claim_notification_success_disable", locale=user_locale),
        cancel_msg=False,
    )


async def show_claim_treshold_info(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
):
    cfg = user_object.get_notification_config()
    usd = cfg.get("usd_threshold", 0) or 0
    tokens = cfg.get("token_thresholds") or {}
    legacy_strk = user_object.claim_reward_msg or 0

    lines: list[str] = []
    if usd > 0:
        lines.append(translate("show_usd_threshold", user_locale).format(amount=usd))
    if tokens:
        for sym, amt in tokens.items():
            if amt > 0:
                lines.append(
                    translate("show_token_threshold", user_locale).format(
                        symbol=sym, amount=amt
                    )
                )
    # Legacy single-number STRK threshold (only if config didn't already cover STRK).
    if legacy_strk and not tokens.get("STRK"):
        lines.append(
            translate("show_token_threshold", user_locale).format(
                symbol="STRK", amount=legacy_strk
            )
        )

    if not lines:
        await message.reply(
            translate("notification_disabled", locale=user_locale), parse_mode="HTML"
        )
        return

    body = translate("show_thresholds_header", user_locale) + "\n" + "\n".join(lines)
    await finish_operation(
        message, state, user_locale, privious_msg=body, cancel_msg=False
    )
