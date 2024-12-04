from aiogram import types
from aiogram.fsm.context import FSMContext

from data.languages import translate
from bot.handlers.start import send_welcome


async def finish_operation(message: types.Message, state: FSMContext, user_locale: str, privious_msg='', cancel_msg=True):
    if cancel_msg:
        finish_msg = f"{privious_msg}\n\n{translate("operation_cancelled", user_locale)}" if privious_msg else translate("operation_cancelled", user_locale)
    else: 
        finish_msg = f"{privious_msg}"
    await send_welcome(message, state, user_locale, cancel_msg=finish_msg)

