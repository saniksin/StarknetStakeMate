from aiogram.filters import BaseFilter
from aiogram.types import Message
from data.languages import translate


class TextFilter(BaseFilter):
    def __init__(self, text: str | list[str]):
        self.text = text if isinstance(text, list) else [text]

    async def __call__(self, message: Message) -> bool:
        return message.text in self.text

class AdminReplyFilter(BaseFilter):
    async def __call__(self, message: Message, user_locale: str = "en") -> bool:
        if message.reply_to_message:
            header = translate("message_from_user", user_locale)
            return header in message.reply_to_message.text
        return False

class UserReplyToAdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        # Перевіряємо, чи є це повідомлення відповіддю на інше повідомлення
        if not message.reply_to_message:
            return False

        # Отримуємо текст оригінального повідомлення, щоб переконатися, що це повідомлення від адміністратора
        original_message = message.reply_to_message
        if not original_message.text:
            return False
        
        if "@" not in original_message.text and "ID:" not in original_message.text:
            return False

        # Перевірка на наявність префікса "Відповідь від адміністратора"
        return original_message
    
