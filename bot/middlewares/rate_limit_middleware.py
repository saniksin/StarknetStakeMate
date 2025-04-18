from aiogram import BaseMiddleware
from aiogram.types import Update
from utils.rate_limiter import RateLimiter


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self):
        self.rate_limiter = RateLimiter()

    async def __call__(self, handler, event: Update, data: dict):
        user_id = event.message.from_user.id
        user_locale = data.get("user_locale", "en")  # Получаем локаль из data
        
        is_allowed, warning_message = self.rate_limiter.is_allowed(user_id, user_locale)
        
        if not is_allowed:
            if warning_message:
                await event.message.reply(warning_message, parse_mode="HTML")
            return
        
        return await handler(event, data) 