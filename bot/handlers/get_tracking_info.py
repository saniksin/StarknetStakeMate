import json
import asyncio
import aiohttp

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data.languages import translate
from db_api.models import Users
from data.contracts import Contracts
from parse.parse_info import parse_delegator_staking_info, parse_validator_staking_info
from utils.msg_format import format_section
from utils.format_decimal import format_decimal
from bot.handlers.clear_state import finish_operation
from utils.logger import logger
from utils.cache import cache, get_cache_key
from utils.queue_manager import queue_manager
from db_api.database import write_to_db, get_account
from data.tg_bot import BOT_TOKEN
from db_api.database import db

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


# Хендлер команды /get_full_info
async def get_tracking_full_info(message: types.Message):
    """
    Обработчик команды полной информации.
    Проверяет наличие запроса в очереди и добавляет новый если нет.
    """
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
            
        # Добавляем запрос в очередь
        request_data = {
            'command': 'full_info'
        }
        user.request_queue = json.dumps(request_data)
        await write_to_db(user)
        
        # Получаем позицию в очереди
        async with AsyncSession(db.engine) as session:
            query = select(Users).where(Users.request_queue.isnot(None))
            result = await session.execute(query)
            users = result.scalars().all()
            position = next((i + 1 for i, u in enumerate(users) if u.user_id == user.user_id), 0)
        
        await message.answer(
            translate("queue_position", user.user_language).format(position=position),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error in get_tracking_full_info: {str(e)}")
        if user:
            await message.answer(
                translate("error_processing_request", user.user_language)
            )


async def process_full_info(user: Users):
    """
    Обработчик для полной информации о стейкинге
    """
    try:
        request_data = json.loads(user.request_queue)
        
        # Проверяем кеш
        cache_key = get_cache_key(user.user_id, "full_info")
        cached_data = await cache.get(cache_key)
        
        if cached_data:
            logger.info(f"Found cached data for user {user.user_id}")
            await send_message(user.user_id, cached_data)
            return

        # Загрузка данных отслеживания пользователя
        if not user.tracking_data or not json.loads(user.tracking_data)['data_pair']:
            logger.warning(f"User {user.user_id} has no tracking data")
            await send_message(user.user_id, translate("no_addresses_to_parse", user.user_language))
            return

        tracking_data = json.loads(user.tracking_data)
        logger.info(f"Starting parsing for {len(tracking_data['data_pair'])} addresses")

        # Определение контрактов для стейкинга
        staking_contracts = {
            Contracts.L2_STAKING_CONTRACT.hex_address, 
            Contracts.L2_STAKING_CONTRACT.hex_address_2, 
            Contracts.L2_STAKING_CONTRACT.hex_address_3
        }

        # Создание списка задач для асинхронного выполнения
        async_tasks = []
        task_info = []

        for index, (address, pool) in enumerate(tracking_data['data_pair']):
            if pool in staking_contracts:
                # Это валидатор
                logger.debug(f"Adding validator parsing task for address {address}")
                async_tasks.append(asyncio.create_task(parse_validator_staking_info(address)))
                task_info.append((index, 'validator', address, pool))
            else:
                # Это делегатор
                logger.debug(f"Adding delegator parsing task for address {address} with pool {pool}")
                async_tasks.append(asyncio.create_task(parse_delegator_staking_info(address, pool)))
                task_info.append((index, 'delegator', address, pool))

        # Ожидание завершения всех задач
        logger.info(f"Starting parsing for {len(async_tasks)} addresses")
        results = await asyncio.gather(*async_tasks)

        # Формирование сообщения с результатами
        response_message = ""

        # Прочитаем результаты в том порядке, в котором они были запущены
        for index, task_result in zip([t[0] for t in task_info], results):
            task_type, address, pool = task_info[index][1:4]  

            if task_result:
                logger.debug(f"Successfully parsed {task_type} data for address {address}")
                if task_type == 'validator':
                    # Формируем информацию для валидатора
                    response_message += format_section(
                        user.user_language, 'validator', task_result, 
                        address, pool, 'validator_info_address', 'staking_info_address'
                    )
                elif task_type == 'delegator':
                    # Формируем информацию для делегатора
                    response_message += format_section(
                        user.user_language, 'delegator', task_result, 
                        address, pool, 'delegator_info_address', 'pool_info_address'
                    )
            else:
                logger.warning(f"Failed to parse {task_type} data for address {address}")
                if task_type == 'validator':
                    # Если нет данных для валидатора
                    response_message += format_section(
                        user.user_language, 'validator', None, 
                        address, pool, 'validator_info_address', 'staking_info_address', no_data=True
                    )
                elif task_type == 'delegator':
                    # Если нет данных для делегатора
                    response_message += format_section(
                        user.user_language, 'delegator', None, 
                        address, pool, 'delegator_info_address', 'pool_info_address', no_data=True
                    )

        # Сохраняем в кеш
        await cache.set(cache_key, response_message)
        
        # Отправляем сообщение
        await send_message(user.user_id, response_message)
        
    except Exception as e:
        logger.error(f"Error processing full info for user {user.user_id}: {str(e)}")
        await send_message(user.user_id, translate("error_processing_request", user.user_language))


# Хендлер команды /get_reward_info
async def get_tracking_reward_info(message: types.Message):
    """
    Обработчик команды информации о наградах.
    Проверяет наличие запроса в очереди и добавляет новый если нет.
    """
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
            
        # Добавляем запрос в очередь
        request_data = {
            'command': 'rewards_info'
        }
        user.request_queue = json.dumps(request_data)
        await write_to_db(user)
        
        # Получаем позицию в очереди
        async with AsyncSession(db.engine) as session:
            query = select(Users).where(Users.request_queue.isnot(None))
            result = await session.execute(query)
            users = result.scalars().all()
            position = next((i + 1 for i, u in enumerate(users) if u.user_id == user.user_id), 0)

        await message.answer(
            translate("queue_position", user.user_language).format(position=position),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error in get_tracking_reward_info: {str(e)}")
        if user:
            await message.answer(
                translate("error_processing_request", user.user_language)
            )


async def process_reward_info(user: Users):
    """
    Обработчик для информации о наградах
    """
    try:
        request_data = json.loads(user.request_queue)
        
        # Проверяем кеш
        cache_key = get_cache_key(user.user_id, "reward_info")
        cached_data = await cache.get(cache_key)
        
        if cached_data:
            logger.info(f"Found cached data for user {user.user_id}")
            await send_message(user.user_id, cached_data)
            return

        # Загрузка данных отслеживания пользователя
        if not user.tracking_data or not json.loads(user.tracking_data)['data_pair']:
            logger.warning(f"User {user.user_id} has no tracking data")
            await send_message(user.user_id, translate("no_addresses_to_parse", user.user_language))
            return

        tracking_data = json.loads(user.tracking_data)
        logger.info(f"Starting parsing for {len(tracking_data['data_pair'])} addresses")

        # Определение контрактов для стейкинга
        staking_contracts = {
            Contracts.L2_STAKING_CONTRACT.hex_address, 
            Contracts.L2_STAKING_CONTRACT.hex_address_2, 
            Contracts.L2_STAKING_CONTRACT.hex_address_3
        }

        # Создание списка задач для асинхронного выполнения
        async_tasks = []
        task_info = []

        for index, (address, pool) in enumerate(tracking_data['data_pair']):
            if pool in staking_contracts:
                # Это валидатор
                logger.debug(f"Adding validator parsing task for address {address}")
                async_tasks.append(asyncio.create_task(parse_validator_staking_info(address)))
                task_info.append((index, 'validator', address, pool))
            else:
                # Это делегатор
                logger.debug(f"Adding delegator parsing task for address {address} with pool {pool}")
                async_tasks.append(asyncio.create_task(parse_delegator_staking_info(address, pool)))
                task_info.append((index, 'delegator', address, pool))

        # Ожидание завершения всех задач
        logger.info(f"Starting parsing for {len(async_tasks)} addresses")
        results = await asyncio.gather(*async_tasks)

        # Формирование сообщения с результатами
        response_message = ""

        # Прочитаем результаты в том порядке, в котором они были запущены
        for index, task_result in zip([t[0] for t in task_info], results):
            task_type, address, pool = task_info[index][1:4]  

            if task_result:
                logger.debug(f"Successfully parsed {task_type} reward data for address {address}")
                if task_type == 'validator':
                    # Формируем информацию для валидатора
                    unclaimed_rewards_own = format_decimal(task_result[0]['unclaimed_rewards_own'])
                    response_message += "\n\n================================\n"
                    response_message += f"{translate('validator_info', user.user_language)}\n"
                    response_message += "================================\n"
                    response_message += f"{translate('reward_address', user.user_language)} <code>{address}</code>\n"
                    response_message += f"{translate('staking_info_address', user.user_language)} <code>{pool}</code>\n"
                    response_message += f"{translate('claim_for_validator', user.user_language).format(amount_1=unclaimed_rewards_own)}\n"
                    response_message += "================================\n"
                elif task_type == 'delegator':
                    # Формируем информацию для делегатора
                    unclaimed_rewards = format_decimal(task_result[0]['unclaimed_rewards'])
                    response_message += "\n\n================================\n"
                    response_message += f"{translate('delegator_info', user.user_language)}\n"
                    response_message += "================================\n"
                    response_message += f"{translate('reward_address', user.user_language)} <code>{address}</code>\n"
                    response_message += f"{translate('pool_info_address', user.user_language)} <code>{pool}</code>\n"
                    response_message += f"{translate('claim_for_delegator', user.user_language).format(amount_1=unclaimed_rewards)}\n"
                    response_message += "================================\n"
            else:
                logger.warning(f"Failed to parse {task_type} reward data for address {address}")
                if task_type == 'validator':
                    # Если нет данных для валидатора
                    response_message += "\n\n================================\n"
                    response_message += f"{translate('validator_info', user.user_language)}\n"
                    response_message += "================================\n"
                    response_message += f"{translate('reward_address', user.user_language)} <code>{address}</code>\n"
                    response_message += f"{translate('staking_info_address', user.user_language)} <code>{pool}</code>\n"
                    response_message += f"{translate('invalid_validator_address', user.user_language)}\n"
                    response_message += "================================\n"
                elif task_type == 'delegator':
                    # Если нет данных для делегатора
                    response_message += "\n\n================================\n"
                    response_message += f"{translate('delegator_info', user.user_language)}\n"
                    response_message += "================================\n"
                    response_message += f"{translate('reward_address', user.user_language)} <code>{address}</code>\n"
                    response_message += f"{translate('pool_info_address', user.user_language)} <code>{pool}</code>\n"
                    response_message += f"{translate('invalid_delegator_address', user.user_language)}\n"
                    response_message += "================================\n"

        # Сохраняем в кеш
        await cache.set(cache_key, response_message)
        
        # Отправляем сообщение
        await send_message(user.user_id, response_message)
        
    except Exception as e:
        logger.error(f"Error processing reward info for user {user.user_id}: {str(e)}")
        await send_message(user.user_id, translate("error_processing_request", user.user_language))


async def get_tracking_validator_info(message: types.Message):
    """
    Обработчик команды информации о валидаторе.
    Проверяет наличие запроса в очереди и добавляет новый если нет.
    """
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
            
        # Добавляем запрос в очередь
        request_data = {
            'command': 'validator_info'
        }
        user.request_queue = json.dumps(request_data)
        await write_to_db(user)
        
        # Получаем позицию в очереди
        async with AsyncSession(db.engine) as session:
            query = select(Users).where(Users.request_queue.isnot(None))
            result = await session.execute(query)
            users = result.scalars().all()
            position = next((i + 1 for i, u in enumerate(users) if u.user_id == user.user_id), 0)

        await message.answer(
            translate("queue_position", user.user_language).format(position=position),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error in get_tracking_validator_info: {str(e)}")
        if user:
            await message.answer(
                translate("error_processing_request", user.user_language)
            )
