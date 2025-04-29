import asyncio
import json
import aiohttp
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db_api.database import db, Users, get_account, write_to_db
from data.languages import translate
from utils.logger import logger
from utils.cache import clear_user_cache
from data.tg_bot import BOT_TOKEN

from bot.handlers.get_tracking_info import process_full_info, process_reward_info
from bot.handlers.info import process_validator_info

TELEGRAM_API_BASE = "https://api.telegram.org/bot"

async def send_message(chat_id: int, text: str):
    """Отправка сообщения через Telegram API."""
    url = f"{TELEGRAM_API_BASE}{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            if response.status != 200:
                logger.error(f"Ошибка отправки сообщения: {await response.text()}")

async def get_users_with_requests() -> List[Users]:
    """Получает список пользователей с активными запросами в очереди"""
    async with AsyncSession(db.engine) as session:
        query = select(Users).where(Users.request_queue.isnot(None))
        result = await session.execute(query)
        return result.scalars().all()

async def process_single_request(user: Users):
    """Обрабатывает один запрос из очереди"""
    try:
        if not user.request_queue:
            return

        try:
            request_data = json.loads(user.request_queue)
            logger.info(f"Processing request for user {user.user_id}: {request_data}")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse request_queue JSON for user {user.user_id}: {e}")
            user.request_queue = None
            await write_to_db(user)
            return

        command = request_data.get('command')
        if not command:
            logger.error(f"No command found in request_data for user {user.user_id}: {request_data}")
            user.request_queue = None
            await write_to_db(user)
            return

        # Проверяем наличие адреса для команд, которые его требуют
        if command == 'validator_info' and not request_data.get('address'):
            logger.debug(f"Waiting for address for validator_info command from user {user.user_id}")
            return

        # Обрабатываем запрос в зависимости от команды
        try:
            if command == 'full_info':
                await process_full_info(user)
            elif command == 'validator_info':
                await process_validator_info(user)
            elif command == 'rewards_info':
                await process_reward_info(user)
            else:
                logger.error(f"Unknown command type: {command} for user {user.user_id}")
                await send_message(
                    chat_id=user.user_id,
                    text=translate("error_processing_request", user.user_language)
                )
        except Exception as e:
            logger.error(f"Error processing command {command} for user {user.user_id}: {str(e)}")
            await send_message(
                chat_id=user.user_id,
                text=translate("error_processing_request", user.user_language)
            )
        finally:
            # Очищаем запрос из БД только если он был обработан
            user.request_queue = None
            await write_to_db(user)
            await clear_user_cache(user.user_id)

    except Exception as e:
        logger.error(f"Critical error processing request for user {user.user_id}: {str(e)}")
        try:
            await send_message(
                chat_id=user.user_id,
                text=translate("error_processing_request", user.user_language)
            )
        except:
            pass

async def process_request_queue():
    """
    Основной цикл обработки очереди запросов.
    Работает независимо от основного потока бота.
    """
    while True:
        try:
            # Получаем список пользователей с запросами
            users = await get_users_with_requests()
            
            # Обрабатываем каждый запрос
            for user in users:
                await process_single_request(user)
            
            # Небольшая пауза между проверками
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"Error in request queue processing: {str(e)}")
            await asyncio.sleep(5) 