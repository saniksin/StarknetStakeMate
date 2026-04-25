"""``/add_info`` FSM — now with a ``Label`` step.

Flow:
    choose_type → (validator: address → label)
                → (delegator: address → pool → label)
                → prepere_confirmation (on-chain check)
                → confirmation (save / cancel)
"""
from __future__ import annotations

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.clear_state import finish_operation
from data.contracts import Contracts
from data.languages import translate
from db_api.database import Users, db, get_user_tracking
from services.staking_service import get_delegator_positions, get_validator_info
from services.tracking_service import dump_tracking, total_tracked
from utils.cache import clear_user_cache
from utils.check_valid_addresses import is_valid_starknet_address
from utils.logger import logger

_MAX_TRACKED = 10  # picker keyboards stay readable up to ~10 rows on mobile


class AddInfoState(StatesGroup):
    choose_type = State()
    awaiting_validator_address = State()
    awaiting_delegate_address = State()
    # Former ``awaiting_pool_address`` is gone — we now ask the user for the
    # validator address instead and enumerate pools automatically.
    awaiting_staker_address = State()
    awaiting_label = State()
    awaiting_prepere_confirmation = State()
    awaiting_confirmation = State()


def _cancel_kb(user_locale: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=translate("cancel", user_locale))]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _skip_kb(user_locale: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("skip_label", user_locale))],
            [KeyboardButton(text=translate("cancel", user_locale))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def _ensure_capacity(message: types.Message, user_object: Users, user_locale: str, state: FSMContext) -> bool:
    data = await get_user_tracking(user_object.user_id)
    if total_tracked(data) >= _MAX_TRACKED:
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate("info_limit_reached", user_locale),
        )
        return False
    return True


async def add_info(message: types.Message, state: FSMContext, user_locale: str) -> None:
    await state.clear()
    options = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("add_delegate_address", user_locale))],
            [KeyboardButton(text=translate("add_validator_address", user_locale))],
            [KeyboardButton(text=translate("cancel", user_locale))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.reply(
        translate("choose_add_type", user_locale), reply_markup=options, parse_mode="HTML"
    )
    await state.set_state(AddInfoState.choose_type)


async def process_add_type(message: types.Message, state: FSMContext, user_locale: str) -> None:
    text = (message.text or "").lower()
    if text == translate("add_validator_address", user_locale).lower():
        await message.reply(
            translate("enter_validator_address", user_locale),
            reply_markup=_cancel_kb(user_locale),
            parse_mode="HTML",
        )
        await state.set_state(AddInfoState.awaiting_validator_address)
    elif text == translate("add_delegate_address", user_locale).lower():
        await message.reply(
            translate("enter_delegate_address", user_locale),
            reply_markup=_cancel_kb(user_locale),
            parse_mode="HTML",
        )
        await state.set_state(AddInfoState.awaiting_delegate_address)
    else:
        await finish_operation(message, state, user_locale)


async def process_validator_address(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
) -> None:
    addr = (message.text or "").strip()
    if not is_valid_starknet_address(addr):
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate("invalid_validator_address", user_locale),
        )
        return
    if not await _ensure_capacity(message, user_object, user_locale, state):
        return
    await state.update_data(
        validator_address=addr,
        pool_address=Contracts.L2_STAKING_CONTRACT.hex_address,
        add_validator=True,
    )
    await _ask_label(message, state, user_locale)


async def process_delegator_address(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
) -> None:
    addr = (message.text or "").strip()
    if not is_valid_starknet_address(addr):
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate("invalid_delegator_address", user_locale),
        )
        return
    if not await _ensure_capacity(message, user_object, user_locale, state):
        return
    await state.update_data(delegetor_address=addr, add_delegator=True)
    await message.reply(
        translate("enter_staker_address_for_delegator", user_locale),
        reply_markup=_cancel_kb(user_locale),
        parse_mode="HTML",
    )
    await state.set_state(AddInfoState.awaiting_staker_address)


async def process_staker_address(
    message: types.Message, state: FSMContext, user_locale: str
) -> None:
    """New step: after the delegator address, the user gives the validator
    (staker) address. The bot enumerates pools automatically.
    """
    staker = (message.text or "").strip()
    if not is_valid_starknet_address(staker):
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate("invalid_validator_address", user_locale),
        )
        return
    await state.update_data(staker_address=staker)
    await _ask_label(message, state, user_locale)


# Kept importable under its old name for anything that still references it.
process_pool_address = process_staker_address


async def _ask_label(message: types.Message, state: FSMContext, user_locale: str) -> None:
    await message.reply(
        translate("enter_label", user_locale),
        reply_markup=_skip_kb(user_locale),
        parse_mode="HTML",
    )
    await state.set_state(AddInfoState.awaiting_label)


async def process_label(
    message: types.Message, state: FSMContext, user_locale: str
) -> None:
    raw = (message.text or "").strip()
    if raw.lower() == translate("cancel", user_locale).lower():
        await finish_operation(message, state, user_locale)
        return
    label = "" if raw.lower() == translate("skip_label", user_locale).lower() else raw
    # Don't let someone inject HTML into their own label — we escape on render,
    # but caping the length keeps buttons readable.
    if len(label) > 40:
        label = label[:40]
    await state.update_data(label=label)
    await state.set_state(AddInfoState.awaiting_prepere_confirmation)
    await confirm_tracking_data(message, state, user_locale)


async def confirm_tracking_data(
    message: types.Message, state: FSMContext, user_locale: str
) -> None:
    data = await state.get_data()
    confirm_message = ""

    if data.get("add_validator"):
        await message.reply(
            translate("check_correct_validator_data", user_locale), parse_mode="HTML"
        )
        result = await get_validator_info(
            data.get("validator_address"), with_attestation=False
        )
        if result is None:
            await state.clear()
            await finish_operation(
                message, state, user_locale,
                privious_msg=translate("incorrect_validator_data", user_locale),
                cancel_msg=False,
            )
            return
        confirm_message = translate(
            "confirm_validator_info", user_locale,
            validator_address=data.get("validator_address"),
            pool_address=data.get("pool_address"),
        )
    elif data.get("add_delegator"):
        await message.reply(
            translate("check_correct_delegator_data", user_locale), parse_mode="HTML"
        )
        multi = await get_delegator_positions(
            data.get("staker_address"), data.get("delegetor_address")
        )
        if multi is None or not multi.has_any:
            await state.clear()
            await finish_operation(
                message, state, user_locale,
                privious_msg=translate("incorrect_delegator_data", user_locale),
                cancel_msg=False,
            )
            return
        confirm_message = translate(
            "confirm_delegate_info", user_locale,
            delegate_address=data.get("delegetor_address"),
            staker_address=data.get("staker_address"),
        )
        # The user already paid for an RPC round-trip — show what we found so
        # they can sanity-check the amounts before saving.
        from services.formatting import _fmt_amount

        pool_lines = []
        for pos in multi.positions:
            sym = pos.token_symbol or "STRK"
            stake = _fmt_amount(pos.amount_decimal, sym)
            # Rewards are always in STRK in V2.
            unclaimed = _fmt_amount(pos.unclaimed_rewards_decimal, "STRK")
            pool_lines.append(f"• {sym}: <b>{stake}</b> · 🎁 {unclaimed}")
        if pool_lines:
            confirm_message += (
                f"\n\n📊 <b>{translate('pools_header', user_locale)} "
                f"({len(multi.positions)})</b>\n" + "\n".join(pool_lines)
            )
    else:
        await finish_operation(message, state, user_locale)
        return

    if data.get("label"):
        confirm_message += f"\n🏷 <b>{translate('label_field', user_locale)}:</b> {data['label']}"

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("save", user_locale))],
            [KeyboardButton(text=translate("cancel", user_locale))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.reply(confirm_message, reply_markup=kb, parse_mode="HTML")
    await state.set_state(AddInfoState.awaiting_confirmation)


async def process_confirmation(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
) -> None:
    data = await state.get_data()
    text = (message.text or "").lower()

    if text == translate("save", user_locale).lower():
        doc = await get_user_tracking(user_object.user_id)

        if data.get("add_validator"):
            doc.setdefault("validators", []).append({
                "address": data["validator_address"],
                "label": data.get("label", ""),
            })
            msg_key = "validator_info_saved"
        else:
            doc.setdefault("delegations", []).append({
                "delegator": data["delegetor_address"],
                "staker": data["staker_address"],
                "label": data.get("label", ""),
            })
            msg_key = "delegate_info_saved"

        user_object.tracking_data = dump_tracking(doc)

        async with AsyncSession(db.engine) as session:
            await session.merge(user_object)
            await session.commit()

        logger.info(f"added tracking entry for {user_object.user_id}")
        await clear_user_cache(user_object.user_id)

        await state.clear()
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate(msg_key, user_locale),
            cancel_msg=False,
        )

    elif text == translate("cancel", user_locale).lower():
        await state.clear()
        await finish_operation(message, state, user_locale)
