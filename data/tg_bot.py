import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher

load_dotenv()

def init_bot():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("BOT_TOKEN is not set in .env")
    bot = Bot(token=bot_token)
    dp = Dispatcher()
    return bot, dp

bot, dp = init_bot()
