"""``/dashboard`` — one compact message with all tracked positions plus
per-entry inline buttons. Falls back to a friendly hint when the user has
nothing tracked yet.
"""
from __future__ import annotations

from aiogram import types

from bot.keyboards import dashboard_grid
from data.languages import translate
from db_api.database import get_account
from services.tracking_service import fetch_tracking_entries, render_dashboard_summary


async def dashboard_command(message: types.Message) -> None:
    user = await get_account(str(message.from_user.id))
    locale = user.user_language if user else "en"
    tracking_data = user.tracking_data if user else None

    entries = await fetch_tracking_entries(tracking_data)
    if not entries:
        await message.answer(
            translate("no_addresses_to_parse", locale), parse_mode="HTML"
        )
        return

    summary = render_dashboard_summary(entries, locale)
    meta = [(e.index, e.kind, e.label or _fallback_name(e)) for e in entries]
    await message.answer(
        summary, parse_mode="HTML", reply_markup=dashboard_grid(meta, locale)
    )


def _fallback_name(entry) -> str:
    head = entry.address[:6]
    tail = entry.address[-4:]
    return f"{head}…{tail}"
