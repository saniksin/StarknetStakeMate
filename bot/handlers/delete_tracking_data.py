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


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-6:]}"


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
        user_object.tracking_data = dump_tracking({"validators": [], "delegations": []})
        async with AsyncSession(db.engine) as session:
            await session.merge(user_object)
            await session.commit()
        await clear_user_cache(user_object.user_id)
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate("all_info_deleted", user_locale),
            cancel_msg=False,
        )
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
    pick = picker.get((message.text or "").strip())
    if not pick:
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate("address_not_found", user_locale),
        )
        return

    kind, idx = pick
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
