from aiogram import BaseMiddleware
from aiogram.types import Update
from utils.exceptions import EventStop
from db_api.user_service import get_or_create_user
from data.languages import translate
from data.tg_bot import bot


class LocaleMiddleware(BaseMiddleware):
    def __init__(self, supported_locales=None, default_locale="en"):
        super().__init__()
        self.supported_locales = supported_locales or ["en", "ru", "ua", "zh", "ko"]
        self.default_locale = default_locale

    async def __call__(self, handler, event: Update, data: dict):
        user = await self._get_user(event, data)
        user_status = await self._check_user_blocked(user, event)
        if user_status:
            return
        return await handler(event, data)

    async def _get_user(self, event: Update, data: dict):
        user_id = event.message.from_user.id
        user_name = event.message.from_user.username or ''
        telegram_language = event.message.from_user.language_code or self.default_locale
        if telegram_language not in self.supported_locales:
            telegram_language = 'en'
        registration_date = event.message.date
        user = await get_or_create_user(
            user_id, user_name, telegram_language, registration_date
        )
        data["user_locale"] = user.user_language
        data["user_object"] = user
        return user

    async def _check_user_blocked(self, user, event: Update):
        if user.user_is_blocked:
            block_text = translate("block_message", locale=user.user_language)
            await bot.send_message(chat_id=event.message.from_user.id, text=block_text, parse_mode="HTML")
            return True
        return False
