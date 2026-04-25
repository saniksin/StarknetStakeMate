"""Centralised inline-keyboard builders for the redesigned bot UI."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from data.languages import translate


# Callback-data grammar:
#   menu:<action>                  — main menu buttons
#   card:<action>:<index>          — per-entry actions
#   add:<kind>                     — kind picker inside /add_info
# Keeping it short because Telegram caps callback_data at 64 bytes.


def main_menu(locale: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text=f"📊 {translate('dashboard_header', locale)}", callback_data="menu:dashboard"),
        InlineKeyboardButton(text=f"➕ {translate('add_info_btn', locale)}", callback_data="menu:add"),
    )
    b.row(
        InlineKeyboardButton(text=f"🔔 {translate('notifications_btn', locale)}", callback_data="menu:notifications"),
        InlineKeyboardButton(text=f"🌐 {translate('language_btn', locale)}", callback_data="menu:language"),
    )
    b.row(
        InlineKeyboardButton(text=f"❓ {translate('help_btn', locale)}", callback_data="menu:help"),
        InlineKeyboardButton(text=f"📞 {translate('contact_admin_btn', locale)}", callback_data="menu:contact"),
    )
    return b.as_markup()


def add_kind_picker(locale: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text=f"🛡 {translate('add_validator_address', locale)}", callback_data="add:validator"),
        InlineKeyboardButton(text=f"🎱 {translate('add_delegate_address', locale)}", callback_data="add:delegator"),
    )
    b.row(InlineKeyboardButton(text=f"❌ {translate('cancel', locale)}", callback_data="add:cancel"))
    return b.as_markup()


def card_actions(index: int, locale: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text=f"🔄 {translate('refresh_btn', locale)}", callback_data=f"card:refresh:{index}"),
        InlineKeyboardButton(text=f"✏️ {translate('rename_btn', locale)}", callback_data=f"card:rename:{index}"),
    )
    b.row(
        InlineKeyboardButton(text=f"❌ {translate('remove_btn', locale)}", callback_data=f"card:remove:{index}"),
    )
    return b.as_markup()


def dashboard_grid(entries_meta: list[tuple[int, str, str]], locale: str) -> InlineKeyboardMarkup:
    """Grid of quick-access buttons, one per tracked entry.

    ``entries_meta`` is a list of ``(index, kind, display_name)`` tuples.
    Layout: 2 columns, kind-icon prefix.
    """
    b = InlineKeyboardBuilder()
    for index, kind, name in entries_meta:
        icon = "🛡" if kind == "validator" else "🎱"
        # Trim overly long labels so the button stays compact.
        trimmed = name if len(name) <= 22 else (name[:19] + "…")
        b.button(text=f"{icon} {trimmed}", callback_data=f"card:refresh:{index}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text=f"🔄 {translate('refresh_all_btn', locale)}", callback_data="menu:dashboard"))
    return b.as_markup()


def confirm(label_yes_key: str, label_no_key: str, prefix: str, index: int, locale: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text=f"✅ {translate(label_yes_key, locale)}", callback_data=f"{prefix}:yes:{index}"),
        InlineKeyboardButton(text=f"❌ {translate(label_no_key, locale)}", callback_data=f"{prefix}:no:{index}"),
    )
    return b.as_markup()


def back_to_menu(locale: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"⬅️ {translate('back_btn', locale)}", callback_data="menu:home"))
    return b.as_markup()
