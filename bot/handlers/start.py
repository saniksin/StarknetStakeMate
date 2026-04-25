import os

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from data.languages import translate


# Определение состояния для главного меню
class MainMenuState(StatesGroup):
    main = State()


# Функция для создания главного меню
def create_main_menu(locale: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
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
        ],
        resize_keyboard=True,
    )


def _dashboard_inline_kb() -> InlineKeyboardMarkup | None:
    """Inline button that opens the Mini App.

    Inline ``web_app`` buttons reliably pass ``initData`` to the Mini App
    on every platform, which ``ReplyKeyboardMarkup`` web_app buttons
    don't do on Telegram Desktop (Telegram-side bug — the keyboard
    button opens the URL but omits ``tgWebAppData`` from the fragment).
    Returns ``None`` when ``WEBAPP_URL`` isn't configured so we don't
    render a broken control.
    """
    url = os.getenv("WEBAPP_URL", "").strip()
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖥 Open Dashboard", web_app=WebAppInfo(url=url))],
        ]
    )


# Хендлер команды /start
async def send_welcome(message: types.Message, state: FSMContext, user_locale: str, cancel_msg=''):
    welcome_text = translate("start_message", locale=user_locale)
    main_menu_kb = create_main_menu(user_locale)

    await message.reply(
        welcome_text if not cancel_msg else cancel_msg,
        reply_markup=main_menu_kb,
        parse_mode="HTML",
    )

    # Mini App entry point — separate message because reply + inline
    # markups can't coexist on a single message. Skip silently if
    # WEBAPP_URL isn't set (local dev without HTTPS).
    dash_kb = _dashboard_inline_kb()
    if dash_kb is not None:
        await message.answer(
            "📊 <b>Dashboard</b> — open the Mini App for a visual overview.",
            reply_markup=dash_kb,
            parse_mode="HTML",
        )

    await state.set_state(MainMenuState.main)
