"""Handler for ``/get_validator_info`` — ask for a staker address, enqueue the
request, render via :mod:`services.formatting` when the worker picks it up.
"""
from __future__ import annotations

import json
import logging

import aiohttp
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.clear_state import finish_operation
from bot.handlers.start import create_main_menu
from data.languages import translate
from data.tg_bot import BOT_TOKEN
from db_api.database import db, get_account, write_to_db
from db_api.models import Users
from services.formatting import render_validator_card
from services.staking_service import get_validator_info as fetch_validator_info
from services.tracking_service import TrackingEntry
from utils.cache import cache, get_cache_key
from utils.check_valid_addresses import is_valid_starknet_address
from utils.logger import logger

_logger = logging.getLogger(__name__)
TELEGRAM_API_BASE = "https://api.telegram.org/bot"


async def send_message(chat_id: int, text: str) -> None:
    url = f"{TELEGRAM_API_BASE}{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            if response.status != 200:
                logger.error(f"sendMessage failed: {await response.text()}")


class ValidatorState(StatesGroup):
    awaiting_address = State()


async def _position_in_queue(user_id: int) -> int:
    async with AsyncSession(db.engine) as session:
        query = select(Users).where(Users.request_queue.isnot(None))
        result = await session.execute(query)
        users = result.scalars().all()
    return next((i + 1 for i, u in enumerate(users) if u.user_id == user_id), 0)


async def get_validator_info_handler(
    message: types.Message, state: FSMContext, user_locale: str
) -> None:
    try:
        user = await get_account(str(message.from_user.id))
        if not user:
            return
        if user.request_queue:
            await message.answer(
                translate("request_already_processing", user.user_language),
                parse_mode="HTML",
            )
            return

        # Offer the user's tracked addresses as quick-tap buttons. Labels are
        # easier to navigate than 64-char hex strings, but Telegram reply
        # buttons send their *text* back as the user's message — so we can't
        # put the label on the button and the address on the wire. Workaround:
        # show the label (or a short address fallback), and stash the
        # ``button_text → full address`` map in FSM state so the next handler
        # can resolve the tap. Falls back to raw-address validation when the
        # user types something instead of tapping.
        doc = user.get_tracking_data()
        quick_buttons: list[list[KeyboardButton]] = []
        label_map: dict[str, str] = {}
        seen: set[str] = set()

        def _short(addr: str) -> str:
            return f"{addr[:8]}…{addr[-4:]}"

        def _add_button(addr: str, label: str) -> None:
            label = (label or "").strip()
            text = label or _short(addr)
            # Disambiguate when two tracked entries share a label.
            if text in label_map and label_map[text] != addr:
                text = f"{label} · {_short(addr)}" if label else _short(addr)
            quick_buttons.append([KeyboardButton(text=text)])
            label_map[text] = addr

        for v in doc.get("validators", []):
            addr = v.get("address")
            if addr and addr not in seen:
                seen.add(addr)
                _add_button(addr, v.get("label") or "")
        # For delegations the validator (staker) address is the useful one to
        # query — that's what /get_validator_info expects. Fall back to the old
        # ``pool`` field for any pre-migration data that didn't get wiped.
        for d in doc.get("delegations", []):
            addr = d.get("staker") or d.get("pool")
            if addr and addr not in seen:
                seen.add(addr)
                _add_button(addr, d.get("label") or "")
        quick_buttons.append([KeyboardButton(text=translate("cancel", user_locale))])

        markup = ReplyKeyboardMarkup(
            keyboard=quick_buttons,
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await state.update_data(label_map=label_map)
        await message.reply(
            translate("enter_validator_address_2", user_locale),
            reply_markup=markup,
            parse_mode="HTML",
        )
        await state.set_state(ValidatorState.awaiting_address)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"get_validator_info_handler failed: {exc}")
        await message.answer(translate("error_processing_request", user_locale))


# Kept under the old name so main.py's register_handlers block keeps working.
get_validator_info = get_validator_info_handler


async def handle_validator_address(
    message: types.Message, state: FSMContext, user_locale: str
) -> None:
    try:
        user = await get_account(str(message.from_user.id))
        if not user:
            return

        if message.text == translate("cancel", user_locale):
            await state.clear()
            await finish_operation(
                message,
                state,
                user_locale,
                privious_msg=translate("operation_cancelled", user_locale),
            )
            return

        # Resolve label taps via the FSM map populated by
        # ``get_validator_info_handler``. If the message is a typed address,
        # the lookup misses and we fall through to validation.
        data = await state.get_data()
        label_map = data.get("label_map") or {}
        address = label_map.get(message.text or "", message.text or "").strip()

        if not is_valid_starknet_address(address):
            await message.reply(
                translate("invalid_validator_address", user_locale), parse_mode="HTML"
            )
            return

        if user.request_queue:
            await message.answer(
                translate("request_already_processing", user.user_language),
                parse_mode="HTML",
            )
            await state.clear()
            return

        user.request_queue = json.dumps(
            {"command": "validator_info", "address": address}
        )
        await write_to_db(user)

        position = await _position_in_queue(user.user_id)
        main_menu_kb = create_main_menu(user_locale)
        await message.reply(
            translate("queue_position", user.user_language, position=position),
            parse_mode="HTML",
            reply_markup=main_menu_kb,
        )
        await state.clear()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"handle_validator_address failed: {exc}")
        await message.reply(
            translate("error_processing_request", user_locale), parse_mode="HTML"
        )
        await state.clear()


async def process_validator_info(user: Users) -> None:
    """Render the validator view when the background worker dequeues a request."""
    try:
        if not user.request_queue:
            return
        request_data = json.loads(user.request_queue)
        address = request_data.get("address")
        if not address:
            await send_message(
                user.user_id,
                translate("error_processing_request", user.user_language),
            )
            return

        # No worker-side caching — the cache dict is per-process and would
        # mask edits made in MainProcess. Render fresh every time.
        info = await fetch_validator_info(address)
        if info is None:
            await send_message(
                user.user_id,
                translate("validator_not_found", user.user_language),
            )
            return
        # Reuse the user's saved label so the card matches what notifications
        # and the portfolio view show. Falls back to short address inside
        # ``render_validator_card`` when the user looked up an untracked one.
        a_lower = address.lower()
        label = ""
        doc = user.get_tracking_data()
        for v in doc.get("validators", []):
            if (v.get("address") or "").lower() == a_lower:
                label = (v.get("label") or "").strip()
                break
        if not label:
            for d in doc.get("delegations", []):
                staker = (d.get("staker") or d.get("pool") or "").lower()
                if staker == a_lower:
                    label = (d.get("label") or "").strip()
                    break
        entry = TrackingEntry(
            index=0, kind="validator", address=address, pool="", label=label, data=info
        )
        body = render_validator_card(entry, user.user_language)
        await send_message(user.user_id, body)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"process_validator_info({user.user_id}): {exc}")
        await send_message(
            user.user_id, translate("error_processing_request", user.user_language)
        )
