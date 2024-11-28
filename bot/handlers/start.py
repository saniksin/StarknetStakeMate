from aiogram import types
from data.languages import translate


# Хендлер команды /start
async def send_welcome(message: types.Message, user_locale: str):
    welcome_text = translate("start_message", locale=user_locale)
    await message.reply(
        welcome_text, 
        parse_mode="HTML"
    )
