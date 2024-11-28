from aiogram import types
from data.languages import translate


# Хендлер команды /help
async def help_command(message: types.Message, user_locale: str):
    help_text = translate("help_message", locale=user_locale)
    await message.reply(help_text, parse_mode="HTML")
