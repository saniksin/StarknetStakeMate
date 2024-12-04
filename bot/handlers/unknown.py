from aiogram import types
from aiogram.fsm.context import FSMContext

from data.languages import translate
from bot.handlers.clear_state import finish_operation


async def unknown_command(message: types.Message, state: FSMContext, user_locale: str):
    """
    Хендлер для обработки неизвестных команд.
    """
    
    msg = translate('invalid_choice', user_locale)

    await finish_operation(
        message, state, user_locale, privious_msg=msg, cancel_msg=False
    )
        