import os

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from data.languages import translate


# Определение состояния для главного меню
class MainMenuState(StatesGroup):
    main = State()


def _dashboard_button() -> KeyboardButton | None:
    """Build the WebApp keyboard button if a public dashboard URL is configured.

    Telegram requires an HTTPS URL for ``web_app=`` keyboard buttons. When
    ``WEBAPP_URL`` isn't set (e.g. local-only dev), we hide the button rather
    than render a broken control.
    """
    url = os.getenv("WEBAPP_URL", "").strip()
    if not url:
        return None
    return KeyboardButton(text="🖥 Dashboard", web_app=WebAppInfo(url=url))


# Функция для создания главного меню
def create_main_menu(locale: str) -> ReplyKeyboardMarkup:
    keyboard = [
        [
            KeyboardButton(text=translate("add_info", locale)),
            KeyboardButton(text=translate("delete_info", locale)),
        ],
        [
            KeyboardButton(text=translate("get_full_info", locale)),
            KeyboardButton(text=translate("get_validator_info", locale)),
        ],
        [
            KeyboardButton(text=translate("get_reward_info", locale)),
            KeyboardButton(text=translate("notifications", locale)),
        ],
        [
            KeyboardButton(text=translate("help", locale)),
            KeyboardButton(text=translate("language", locale)),
        ],
        [
            KeyboardButton(text=translate("contact_admin", locale)),
        ],
    ]
    dash = _dashboard_button()
    if dash is not None:
        keyboard.insert(0, [dash])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


# Хендлер команды /start
async def send_welcome(message: types.Message, state: FSMContext, user_locale: str, cancel_msg=''):
    welcome_text = translate("start_message", locale=user_locale)
    main_menu_kb = create_main_menu(user_locale)
    
    await message.reply(
        welcome_text if not cancel_msg else cancel_msg,
        reply_markup=main_menu_kb,
        parse_mode="HTML"
    )
    await state.set_state(MainMenuState.main)
