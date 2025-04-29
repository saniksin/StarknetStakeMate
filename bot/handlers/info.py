from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
import asyncio
import json
import logging
import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from data.languages import translate
from parse.parse_info import parse_validator_staking_info
from utils.msg_format import parse_validator_info, format_section
from utils.check_valid_addresses import is_valid_starknet_address
from bot.handlers.clear_state import finish_operation
from utils.cache import cache, get_cache_key
from utils.logger import logger
from db_api.models import Users
from data.contracts import Contracts
from db_api.database import write_to_db, get_account
from data.tg_bot import BOT_TOKEN
from db_api.database import db
from bot.handlers.start import create_main_menu


logger = logging.getLogger(__name__)

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


class ValidatorState(StatesGroup):
    awaiting_address = State()


async def get_validator_info(message: types.Message, state: FSMContext, user_locale: str):
    try:
        user = await get_account(str(message.from_user.id))
        if not user:
            return
            
        # Проверяем есть ли уже запрос в очереди
        if user.request_queue:
            await message.answer(
                translate("request_already_processing", user.user_language),
                parse_mode="HTML"
            )
            return
            
        markup = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="0x0475a1ba31db59f0eda3b3b260ad3abb30a2a67983cd51d753fdb4adad92a524")],
                [KeyboardButton(text=translate("cancel", locale=user_locale))]
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )

        # Запрос адреса валидатора
        await message.reply(
            translate("enter_validator_address_2", locale=user_locale),
            reply_markup=markup,
            parse_mode="HTML"
        )
        await state.set_state(ValidatorState.awaiting_address)
        
    except Exception as e:
        logger.error(f"Error in get_validator_info: {str(e)}")
        await message.answer(
            translate("error_processing_request", user_locale)
        )


async def handle_validator_address(message: types.Message, state: FSMContext, user_locale: str):
    try:
        user = await get_account(str(message.from_user.id))
        if not user:
            return

        if message.text == translate("cancel", locale=user_locale):
            await state.clear()
            await finish_operation(
                message, state, user_locale, privious_msg=f"{translate('operation_cancelled', user_locale)}"
            )
            return

        # Проверяем валидность адреса
        if not is_valid_starknet_address(message.text):
            await message.reply(
                translate("invalid_validator_address", user_locale),
                parse_mode="HTML"
            )
            return

        # Проверяем есть ли уже запрос в очереди
        if user.request_queue:
            await message.answer(
                translate("request_already_processing", user.user_language),
                parse_mode="HTML"
            )
            await state.clear()
            return

        # Создаем новый запрос
        request_data = {
            'command': 'validator_info',
            'address': message.text
        }
        logger.info(f"Creating new request for user {user.user_id}: {request_data}")
        user.request_queue = json.dumps(request_data)
        await write_to_db(user)

        # Получаем позицию в очереди
        async with AsyncSession(db.engine) as session:
            query = select(Users).where(Users.request_queue.isnot(None))
            result = await session.execute(query)
            users = result.scalars().all()
            position = next((i + 1 for i, u in enumerate(users) if u.user_id == user.user_id), 0)

        main_menu_kb = create_main_menu(user_locale)
        await message.reply(
            translate("queue_position", user.user_language).format(position=position),
            parse_mode="HTML",
            reply_markup=main_menu_kb
        )
        
        await state.clear()

    except Exception as e:
        logger.error(f"Error in handle_validator_address: {str(e)}")
        await message.reply(
            translate("error_processing_request", user_locale),
            parse_mode="HTML"
        )
        await state.clear()


async def process_validator_info(user: Users):
    """
    Обработчик для информации о валидаторе
    """
    try:
        if not user.request_queue:
            return

        request_data = json.loads(user.request_queue)
        address = request_data.get('address')
        
        if not address:
            logger.error(f"No address in request data for user {user.user_id}")
            await send_message(
                user.user_id,
                translate("error_processing_request", user.user_language)
            )
            return

        # Проверяем кеш
        cache_key = get_cache_key(user.user_id, f"validator_info_{address}")
        cached_data = await cache.get(cache_key)
        
        if cached_data:
            logger.info(f"Found cached data for user {user.user_id}")
            await send_message(user.user_id, cached_data)
            return

        # Получаем информацию о валидаторе
        validator_info = await parse_validator_staking_info(address)
        
        if validator_info:
            # Форматируем ответ
            response_message = format_section(
                user.user_language, 'validator', validator_info,
                address, Contracts.L2_STAKING_CONTRACT.hex_address,
                'validator_info_address', 'staking_info_address'
            )
            
            # Сохраняем в кеш
            await cache.set(cache_key, response_message)
            
            # Отправляем сообщение
            await send_message(user.user_id, response_message)
        else:
            await send_message(
                user.user_id,
                translate("invalid_validator_address", user.user_language)
            )

    except Exception as e:
        logger.error(f"Error processing validator info for user {user.user_id}: {str(e)}")
        await send_message(
            user.user_id,
            translate("error_processing_request", user.user_language)
        )
