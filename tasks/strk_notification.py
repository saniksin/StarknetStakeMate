import sys
import asyncio
import json
import aiohttp
from db_api.database import get_strk_notification_users, write_to_db
from db_api.models import Users
from data.languages import translate
from data.models import semaphore, get_admins
from parse.parse_info import parse_validator_staking_info, parse_delegator_staking_info
from utils.format_decimal import format_decimal
from data.contracts import Contracts
from data.tg_bot import BOT_TOKEN
from utils.cache import clear_user_cache
from utils.logger import logger
from sqlalchemy.ext.asyncio import AsyncSession
from db_api.database import db


TELEGRAM_API_BASE = "https://api.telegram.org/bot"


async def send_message(chat_id, text):
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
                print(f"Ошибка отправки сообщения: {await response.text()}")


async def start_parse_and_send_notification(user: Users):
    async with semaphore:
        tracking_data = json.loads(user.tracking_data)

        user_pool_parse_task = []
        response_message = ""

        # Определяем контракты для стейкинга
        staking_contracts = {
            Contracts.L2_STAKING_CONTRACT.hex_address, 
            Contracts.L2_STAKING_CONTRACT.hex_address_2, 
            Contracts.L2_STAKING_CONTRACT.hex_address_3
        }

        # Создаем асинхронные задачи
        for address, pool in tracking_data['data_pair']:
            if pool in staking_contracts:
                # Валидатор
                user_pool_parse_task.append(asyncio.create_task(parse_validator_staking_info(address)))
            else:
                # Делегатор
                user_pool_parse_task.append(asyncio.create_task(parse_delegator_staking_info(address, pool)))

        # Ожидаем завершения всех задач
        results = await asyncio.gather(*user_pool_parse_task)

        message_welcome = f"{translate('strk_notification_msg', user.user_language)}\n"

        # Обрабатываем результаты
        for task_result, (address, pool) in zip(results, tracking_data['data_pair']):
            if task_result:
                result_type = 'validator' if pool in staking_contracts else 'delegator'

                if result_type == 'validator':
                    unclaimed_rewards_own = format_decimal(task_result[0]['unclaimed_rewards_own'])
                    if float(unclaimed_rewards_own) >= user.claim_reward_msg:
                        if message_welcome not in response_message:
                            response_message += message_welcome

                        response_message += "\n================================\n"
                        response_message += f"{translate('validator_info', user.user_language)}\n"
                        response_message += "================================\n"
                        response_message += f"{translate('reward_address', user.user_language)} <code>{address}</code>\n"
                        response_message += f"{translate('staking_info_address', user.user_language)} <code>{pool}</code>\n"
                        response_message += f"{translate('claim_for_validator', user.user_language).format(
                            amount_1=unclaimed_rewards_own)}"

                elif result_type == 'delegator':
                    unclaimed_rewards = format_decimal(task_result[0]['unclaimed_rewards'])
                    if float(unclaimed_rewards) >= user.claim_reward_msg:
                        if message_welcome not in response_message:
                            response_message += message_welcome

                        response_message += "\n================================\n"
                        response_message += f"{translate('delegator_info', user.user_language)}\n"
                        response_message += "================================\n"
                        response_message += f"{translate('reward_address', user.user_language)} <code>{address}</code>\n"
                        response_message += f"{translate('pool_info_address', user.user_language)} <code>{pool}</code>\n"
                        response_message += f"{translate('claim_for_delegator', user.user_language).format(
                            amount_1=unclaimed_rewards)}\n"

        # Если есть уведомления для отправки
        if response_message:
            await send_message(chat_id=user.user_id, text=response_message)

        # Обновляем информацию о клейме в базе данных
        await write_to_db(user)


async def send_strk_notification():
    while True:
        try:
            result_users = await get_strk_notification_users()
            final_user_list = []
            if result_users:
                for user in result_users:
                    tracking_data = json.loads(user.tracking_data)

                    if len(tracking_data['data_pair']) == 0:
                        user.claim_reward_msg = 0
                        await write_to_db(user)
                        await send_message(chat_id=user.user_id, text=translate("no_addresses_to_parse_info", user.user_language))
                    else:
                        final_user_list.append(user)

            print(f'Длинна списка с уведомлениями - {len(final_user_list)}')
            notification_task = []
            if final_user_list:
                for user in final_user_list:
                    notification_task.append(asyncio.create_task(start_parse_and_send_notification(user)))

                await asyncio.gather(*notification_task)

            await asyncio.sleep(3600)
        except Exception as e:
            admins = get_admins()
            error_message = f"Ошибка в процессе уведомлений: {str(e)}\nТип ошибки: {type(e).__name__}"
            logger.error(error_message)
            await send_message(chat_id=admins[0], text=error_message)
            await asyncio.sleep(3600)


async def update_user_notification_data(user_id: int, new_data: dict):
    """Обновляет данные уведомлений пользователя"""
    try:
        async with AsyncSession(db.engine) as session:
            user = await session.get(Users, user_id)
            if user:
                user.notification_data = json.dumps(new_data)
                await session.commit()
                
                # Очищаем кеш пользователя после обновления данных
                logger.info(f"Clearing cache for user {user_id} after updating notification data")
                await clear_user_cache(user_id)
                
                return True
    except Exception as e:
        logger.error(f"Error updating notification data for user {user_id}: {str(e)}")
    return False
