from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

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


# Хендлер команды /start
async def send_welcome(message: types.Message, state: FSMContext, user_locale: str, cancel_msg=''):
    """Send welcome + main reply keyboard.

    Mini App entry point lives outside this handler — it's the BotFather
    Menu Button (the blue button left of the message input), configured
    once via ``@BotFather → /mybots → Bot Settings → Menu Button``. Doing
    it that way keeps the entry permanently visible and avoids the
    Telegram Desktop bug where reply-keyboard ``web_app`` buttons don't
    pass ``initData``.
    """
    welcome_text = translate("start_message", locale=user_locale)
    main_menu_kb = create_main_menu(user_locale)

    await message.reply(
        welcome_text if not cancel_msg else cancel_msg,
        reply_markup=main_menu_kb,
        parse_mode="HTML",
    )
    await state.set_state(MainMenuState.main)
