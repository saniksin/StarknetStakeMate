"""``/delete_info`` — new schema aware, shows labels in the picker."""
from __future__ import annotations

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.clear_state import finish_operation
from data.languages import translate
from db_api.database import Users, db, get_user_tracking
from services.tracking_service import dump_tracking, total_tracked
from utils.cache import clear_user_cache
from utils.logger import logger


class DeleteInfoState(StatesGroup):
    choose_delete_type = State()
    awaiting_selection = State()
    confirm_delete_all = State()
    confirm_delete_specific = State()


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-6:]}"


def _yes_no_kb(user_locale: str) -> ReplyKeyboardMarkup:
    """Yes/No reply keyboard used by the delete confirmation steps."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=translate("yes", user_locale)),
                KeyboardButton(text=translate("no", user_locale)),
            ],
            [KeyboardButton(text=translate("cancel", user_locale))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def start_delete_info(
    message: types.Message, state: FSMContext, user_locale: str
) -> None:
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("delete_all_addresses", user_locale))],
            [KeyboardButton(text=translate("delete_specific_address", user_locale))],
            [KeyboardButton(text=translate("cancel", user_locale))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.reply(
        translate("choose_delete_option", user_locale),
        reply_markup=kb,
        parse_mode="HTML",
    )
    await state.set_state(DeleteInfoState.choose_delete_type)


async def process_delete_choice(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
) -> None:
    text = (message.text or "").lower()
    doc = await get_user_tracking(user_object.user_id)

    if text == translate("delete_all_addresses", user_locale).lower():
        if not total_tracked(doc):
            await finish_operation(
                message, state, user_locale,
                privious_msg=translate("no_addresses_to_delete", user_locale),
            )
            return
        # Confirm before wiping the entire list — easy to mis-tap on the
        # main delete menu and lose every address you've added. ``t_n``
        # threads the count through the right plural template so ru/ua/pl
        # get the noun in the correct case.
        from services.i18n_plural import t_n
        count = total_tracked(doc)
        await message.reply(
            t_n("confirm_delete_all_prompt", count, user_locale, count=count),
            reply_markup=_yes_no_kb(user_locale),
            parse_mode="HTML",
        )
        await state.set_state(DeleteInfoState.confirm_delete_all)
        return

    if text == translate("delete_specific_address", user_locale).lower():
        if not total_tracked(doc):
            await finish_operation(
                message, state, user_locale,
                privious_msg=translate("no_addresses_to_delete", user_locale),
            )
            return

        # Build a reverse map: button text → ("validator"|"delegator", index in its list)
        rows: list[list[KeyboardButton]] = []
        picker: dict[str, tuple[str, int]] = {}

        for i, v in enumerate(doc.get("validators", [])):
            name = v.get("label") or _short(v["address"])
            label = f"🛡 {name}"
            picker[label] = ("validator", i)
            rows.append([KeyboardButton(text=label)])

        for i, d in enumerate(doc.get("delegations", [])):
            addr = d.get("delegator") or d.get("address", "")
            name = d.get("label") or _short(addr)
            label = f"🎱 {name}"
            picker[label] = ("delegator", i)
            rows.append([KeyboardButton(text=label)])

        rows.append([KeyboardButton(text=translate("cancel", user_locale))])

        await state.update_data(picker=picker)
        await state.set_state(DeleteInfoState.awaiting_selection)
        await message.reply(
            translate("choose_address_to_delete", user_locale),
            reply_markup=ReplyKeyboardMarkup(
                keyboard=rows, resize_keyboard=True, one_time_keyboard=True
            ),
            parse_mode="HTML",
        )
        return

    await finish_operation(message, state, user_locale)


async def delete_specific_address(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
) -> None:
    data = await state.get_data()
    picker: dict[str, tuple[str, int]] = data.get("picker", {})
    picked_label = (message.text or "").strip()
    pick = picker.get(picked_label)
    if not pick:
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate("address_not_found", user_locale),
        )
        return

    # Park the choice in FSM and ask for confirmation. Acting on a tap is
    # too easy here because the pickerlist is composed of close-together
    # validator names — adding a Yes/No step turns "oops" into "still a
    # mistake but recoverable".
    await state.update_data(pending_delete=list(pick), pending_label=picked_label)
    await state.set_state(DeleteInfoState.confirm_delete_specific)
    await message.reply(
        translate("confirm_delete_specific_prompt", user_locale).format(label=picked_label),
        reply_markup=_yes_no_kb(user_locale),
        parse_mode="HTML",
    )


async def confirm_delete_all(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
) -> None:
    text = (message.text or "").strip().lower()
    if text != translate("yes", user_locale).lower():
        # No / Cancel / anything else → bail out cleanly without touching
        # tracking_data.
        await finish_operation(message, state, user_locale)
        return

    user_object.tracking_data = dump_tracking({"validators": [], "delegations": []})
    async with AsyncSession(db.engine) as session:
        await session.merge(user_object)
        await session.commit()
    await clear_user_cache(user_object.user_id)
    logger.info(f"deleted all tracking entries for {user_object.user_id}")
    await finish_operation(
        message, state, user_locale,
        privious_msg=translate("all_info_deleted", user_locale),
        cancel_msg=False,
    )


async def confirm_delete_specific(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
) -> None:
    text = (message.text or "").strip().lower()
    if text != translate("yes", user_locale).lower():
        await finish_operation(message, state, user_locale)
        return

    data = await state.get_data()
    pending = data.get("pending_delete")
    if not pending or len(pending) != 2:
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate("address_not_found", user_locale),
        )
        return

    kind, idx = pending[0], int(pending[1])
    doc = await get_user_tracking(user_object.user_id)
    lst_key = "validators" if kind == "validator" else "delegations"
    try:
        doc[lst_key].pop(idx)
    except IndexError:
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate("address_not_found", user_locale),
        )
        return

    user_object.tracking_data = dump_tracking(doc)
    async with AsyncSession(db.engine) as session:
        await session.merge(user_object)
        await session.commit()

    logger.info(f"removed tracking entry for {user_object.user_id}")
    await clear_user_cache(user_object.user_id)
    await finish_operation(
        message, state, user_locale,
        privious_msg=translate("address_deleted", user_locale),
        cancel_msg=False,
    )
