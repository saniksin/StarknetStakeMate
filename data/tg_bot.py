import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher

load_dotenv()

def init_bot(BOT_TOKEN):
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set in .env")
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    return bot, dp

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot, dp = init_bot(BOT_TOKEN)
