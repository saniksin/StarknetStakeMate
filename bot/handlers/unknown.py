from aiogram import types
from data.languages import translate


async def unknown_command(message: types.Message, user_locale: str):
    """
    Хендлер для обработки неизвестных команд.
    """
    await message.reply(
        translate("invalid_choice", user_locale),
        parse_mode="HTML"
    )
