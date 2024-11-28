from aiogram import types
from aiogram.fsm.context import FSMContext

from data.languages import translate

async def cancel_operation(message: types.Message, state: FSMContext, user_locale: str):
    await state.clear()
    await message.reply(translate("operation_cancelled", user_locale), reply_markup=types.ReplyKeyboardRemove(), parse_mode="HTML")

