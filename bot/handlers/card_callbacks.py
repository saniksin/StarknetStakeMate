"""Inline-button callback dispatcher for per-entry card actions.

Handles ``card:refresh|remove|rename:<index>`` and a few menu callbacks.
Keeps the logic centralized so we can wire additional buttons without
spawning more handler modules.
"""
from __future__ import annotations

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.rename_info import RenameState
from bot.keyboards import card_actions
from data.languages import translate
from db_api.database import Users, db, get_account
from services.formatting import render_delegator_card, render_validator_card
from services.tracking_service import dump_tracking, fetch_tracking_entries, total_tracked
from utils.cache import clear_user_cache
from utils.logger import logger


async def on_card_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    """One entry point for ``card:*`` callbacks."""
    try:
        _, action, idx_str = callback.data.split(":", 2)
        index = int(idx_str)
    except (ValueError, AttributeError):
        await callback.answer("bad payload")
        return

    user = await get_account(str(callback.from_user.id))
    if user is None:
        await callback.answer("user unknown")
        return
    locale = user.user_language or "en"

    entries = await fetch_tracking_entries(user.tracking_data)
    entry = next((e for e in entries if e.index == index), None)
    if entry is None:
        await callback.answer(translate("address_not_found", locale), show_alert=True)
        return

    if action == "refresh":
        if entry.kind == "validator":
            body = render_validator_card(entry, locale)
        else:
            body = render_delegator_card(entry, locale)
        try:
            await callback.message.edit_text(
                body, parse_mode="HTML", reply_markup=card_actions(index, locale)
            )
        except Exception:  # noqa: BLE001
            # If the original message wasn't a card (e.g. digest), just send a new one.
            await callback.message.answer(
                body, parse_mode="HTML", reply_markup=card_actions(index, locale)
            )
        await callback.answer(translate("refreshed", locale))
        return

    if action == "remove":
        doc = user.get_tracking_data()
        lst_key = "validators" if entry.kind == "validator" else "delegations"
        # Find the position inside its own list (index is global across both).
        local_idx = index if entry.kind == "validator" else index - len(doc.get("validators", []))
        try:
            removed = doc[lst_key].pop(local_idx)
        except (IndexError, KeyError):
            await callback.answer(translate("address_not_found", locale), show_alert=True)
            return
        user.tracking_data = dump_tracking(doc)
        async with AsyncSession(db.engine) as session:
            await session.merge(user)
            await session.commit()
        await clear_user_cache(user.user_id)
        logger.info(f"removed entry via callback for {user.user_id}: {removed}")
        try:
            await callback.message.edit_text(
                translate("address_deleted", locale), parse_mode="HTML"
            )
        except Exception:  # noqa: BLE001
            await callback.message.answer(translate("address_deleted", locale))
        await callback.answer(translate("address_deleted", locale))
        return

    if action == "rename":
        await state.set_state(RenameState.awaiting_new_label)
        await state.update_data(target_kind=entry.kind, target_idx=_local_idx(entry, user))
        await callback.message.answer(
            translate("enter_new_label", locale), parse_mode="HTML"
        )
        await callback.answer()
        return

    await callback.answer()


def _local_idx(entry, user: Users) -> int:
    doc = user.get_tracking_data()
    if entry.kind == "validator":
        return entry.index
    return entry.index - len(doc.get("validators", []))


async def on_menu_dashboard_callback(callback: types.CallbackQuery) -> None:
    """``menu:dashboard`` — re-render the dashboard view in place."""
    from bot.handlers.dashboard import dashboard_command

    # Fabricate a minimal Message-like for dashboard_command; simpler to just
    # send a fresh message via answer(). Keeping the old one for history.
    fake = callback.message
    fake.from_user = callback.from_user  # override since callback.message.from_user is the bot
    await dashboard_command(fake)
    await callback.answer()
