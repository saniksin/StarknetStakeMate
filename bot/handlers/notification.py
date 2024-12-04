from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from data.languages import translate
from bot.handlers.strk_notification import (
    start_set_threshold,
    set_claim_threshold,
    clear_claim_threshold,
    show_claim_treshold_info
)

# Создаем клавиатуру для меню Notification
def create_notification_menu(locale: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("set_strk_notification", locale))],
            [KeyboardButton(text=translate("cancel", locale))]
        ],
        resize_keyboard=True
    )


# Хендлер для открытия меню Notification
async def open_notification_menu(message: types.Message, state: FSMContext, user_locale: str):
    notification_menu_kb = create_notification_menu(user_locale)
    await message.reply(
        text=translate("notification_menu_title", locale=user_locale),
        reply_markup=notification_menu_kb,
        parse_mode="HTML"
    )
