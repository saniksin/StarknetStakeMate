from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext

from bot.handlers.strk_notification import _attestation_summary_label
from data.languages import translate
from db_api.database import Users
from services.tracking_service import load_tracking


def create_notification_menu(
    locale: str, attestation_summary: str | None = None
) -> ReplyKeyboardMarkup:
    """Top-level notification menu.

    Two independent channels live here as siblings — neither nests inside
    the other:
      - STRK reward thresholds (USD / per-token)
      - Attestation alerts (per-validator opt-in)
    """
    attestation_caption = attestation_summary or translate("attestation_toggle", locale)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("set_strk_notification", locale))],
            [KeyboardButton(text=attestation_caption)],
            [KeyboardButton(text=translate("cancel", locale))],
        ],
        resize_keyboard=True,
    )


async def open_notification_menu(
    message: types.Message,
    state: FSMContext,
    user_locale: str,
    user_object: Users,
):
    cfg = user_object.get_notification_config()
    doc = load_tracking(user_object.tracking_data)
    summary = _attestation_summary_label(cfg, doc.get("validators", []), user_locale)
    notification_menu_kb = create_notification_menu(user_locale, summary)
    await message.reply(
        text=translate("notification_menu_title", locale=user_locale),
        reply_markup=notification_menu_kb,
        parse_mode="HTML",
    )
