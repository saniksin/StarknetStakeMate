"""``/rename`` — assign or change a label for an already-tracked address.

FSM:
    awaiting_selection → awaiting_new_label
"""
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


class RenameState(StatesGroup):
    awaiting_selection = State()
    awaiting_new_label = State()


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-6:]}"


async def start_rename(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
) -> None:
    doc = await get_user_tracking(user_object.user_id)
    if not total_tracked(doc):
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate("no_addresses_to_parse", user_locale),
        )
        return

    rows: list[list[KeyboardButton]] = []
    picker: dict[str, tuple[str, int]] = {}

    for i, v in enumerate(doc.get("validators", [])):
        name = v.get("label") or _short(v["address"])
        btn = f"🛡 {name}"
        picker[btn] = ("validator", i)
        rows.append([KeyboardButton(text=btn)])

    for i, d in enumerate(doc.get("delegations", [])):
        addr = d.get("delegator") or d.get("address", "")
        name = d.get("label") or _short(addr)
        btn = f"🎱 {name}"
        picker[btn] = ("delegator", i)
        rows.append([KeyboardButton(text=btn)])

    rows.append([KeyboardButton(text=translate("cancel", user_locale))])

    await state.update_data(picker=picker)
    await state.set_state(RenameState.awaiting_selection)
    await message.reply(
        translate("rename_prompt", user_locale),
        reply_markup=ReplyKeyboardMarkup(
            keyboard=rows, resize_keyboard=True, one_time_keyboard=True
        ),
        parse_mode="HTML",
    )


async def process_rename_selection(
    message: types.Message, state: FSMContext, user_locale: str
) -> None:
    if (message.text or "").lower() == translate("cancel", user_locale).lower():
        await finish_operation(message, state, user_locale)
        return
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
    await state.update_data(target_kind=kind, target_idx=idx)
    await state.set_state(RenameState.awaiting_new_label)
    await message.reply(
        translate("enter_new_label", user_locale),
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=translate("cancel", user_locale))]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
        parse_mode="HTML",
    )


async def process_new_label(
    message: types.Message, state: FSMContext, user_locale: str, user_object: Users
) -> None:
    text = (message.text or "").strip()
    if text.lower() == translate("cancel", user_locale).lower():
        await finish_operation(message, state, user_locale)
        return

    label = text[:40]  # same cap as in /add_info
    data = await state.get_data()
    kind: str = data["target_kind"]
    idx: int = data["target_idx"]

    doc = await get_user_tracking(user_object.user_id)
    lst_key = "validators" if kind == "validator" else "delegations"
    try:
        doc[lst_key][idx]["label"] = label
    except (IndexError, KeyError):
        await finish_operation(
            message, state, user_locale,
            privious_msg=translate("address_not_found", user_locale),
        )
        return

    user_object.tracking_data = dump_tracking(doc)
    async with AsyncSession(db.engine) as session:
        await session.merge(user_object)
        await session.commit()

    logger.info(f"renamed tracking entry for {user_object.user_id} → {label!r}")
    await clear_user_cache(user_object.user_id)
    await state.clear()
    await finish_operation(
        message, state, user_locale,
        privious_msg=translate("label_saved", user_locale, label=label),
        cancel_msg=False,
    )
