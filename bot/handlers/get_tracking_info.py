import json
import asyncio

from aiogram import types
from aiogram.fsm.context import FSMContext

from data.languages import translate
from db_api.models import Users
from data.contracts import Contracts
from parse.parse_info import parse_delegator_staking_info, parse_validator_staking_info
from utils.msg_format import format_section
from utils.format_decimal import format_decimal
from bot.handlers.clear_state import finish_operation
from utils.logger import logger


# Хендлер команды /get_full_info
async def get_tracking_full_info(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    logger.info(f"User {message.from_user.id} requested full tracking info")
    
    # Загрузка данных отслеживания пользователя
    if user_object.tracking_data:
        tracking_data = json.loads(user_object.tracking_data)
        logger.debug(f"User {message.from_user.id} tracking data loaded: {len(tracking_data['data_pair'])} addresses")
    else:
        logger.warning(f"User {message.from_user.id} has no tracking data")
        await finish_operation(
            message, state, user_locale, privious_msg=f"{translate("tracking_data_empty", user_locale)}"
        )
        return
    
    if len(tracking_data['data_pair']) == 0:
        logger.warning(f"User {message.from_user.id} has empty tracking data list")
        await finish_operation(
            message, state, user_locale, privious_msg=f"{translate("no_addresses_to_parse", user_locale)}"
        )
        return

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
        task_type, address, pool = task_info[index][1], task_info[index][2], task_info[index][3]

        if task_result:
            logger.debug(f"Successfully parsed {task_type} data for address {address}")
            if task_type == 'validator':
                # Формируем информацию для валидатора
                response_message += format_section(
                    user_locale, 'validator', task_result, 
                    address, pool, 'validator_info_address', 'staking_info_address'
                )
            elif task_type == 'delegator':
                # Формируем информацию для делегатора
                response_message += format_section(
                    user_locale, 'delegator', task_result, 
                    address, pool, 'delegator_info_address', 'pool_info_address'
                )
        else:
            logger.warning(f"Failed to parse {task_type} data for address {address}")
            if task_type == 'validator':
                # Если нет данных для валидатора
                response_message += format_section(
                    user_locale, 'validator', None, 
                    address, pool, 'validator_info_address', 'staking_info_address', no_data=True
                )
            elif task_type == 'delegator':
                # Если нет данных для делегатора
                response_message += format_section(
                    user_locale, 'delegator', None, 
                    address, pool, 'delegator_info_address', 'pool_info_address', no_data=True
                )

    # Отправляем пользователю сообщение
    if response_message:
        logger.info(f"Successfully sent full tracking info to user {message.from_user.id}")
        await message.reply(response_message, parse_mode="HTML")
    else:
        logger.warning(f"No tracking info to send to user {message.from_user.id}")
        await message.reply(translate("no_tracking_info", user_locale), parse_mode="HTML")

        
# Хендлер команды /get_reward_info
async def get_tracking_reward_info(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    logger.info(f"User {message.from_user.id} requested reward info")
    
    # Загрузка данных отслеживания пользователя
    if user_object.tracking_data:
        tracking_data = json.loads(user_object.tracking_data)
        logger.debug(f"User {message.from_user.id} tracking data loaded: {len(tracking_data['data_pair'])} addresses")
    else:
        logger.warning(f"User {message.from_user.id} has no tracking data")
        await finish_operation(
            message, state, user_locale, privious_msg=f"{translate("tracking_data_empty", user_locale)}"
        )
        return

    if len(tracking_data['data_pair']) == 0:
        logger.warning(f"User {message.from_user.id} has empty tracking data list")
        await finish_operation(
            message, state, user_locale, privious_msg=f"{translate("no_addresses_to_parse", user_locale)}"
        )
        return

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
        task_type, address, pool = task_info[index][1], task_info[index][2], task_info[index][3]

        if task_result[0]:
            logger.debug(f"Successfully parsed {task_type} reward data for address {address}")
            # Получаем первый элемент кортежа, который является OrderedDict
            task_data = task_result[0]  # task_result это кортеж, берем первый элемент

            if task_type == 'validator':
                # Формируем информацию только по реварду для валидатора
                unclaimed_rewards_own = f"{format_decimal(task_data['unclaimed_rewards_own'])} STRK"
                response_message += "\n================================\n"
                response_message += f"<b>{translate('validator_info_2', user_locale)}</b>\n"
                response_message += "================================\n"
                response_message += f"{translate('reward_address', user_locale)} <code>{address}</code>\n"
                response_message += f"{translate('staking_info_address', user_locale)} <code>{pool}</code>\n"
                response_message += f"{translate('unclaimed_rewards_own', user_locale)} {unclaimed_rewards_own}\n"
            elif task_type == 'delegator':
                # Формируем информацию только по реварду для делегатора
                unclaimed_rewards = f"{format_decimal(task_data['unclaimed_rewards'])} STRK"
                response_message += "\n================================\n"
                response_message += f"<b>{translate('delegator_info', user_locale)}</b>\n"
                response_message += "================================\n"
                response_message += f"{translate('reward_address', user_locale)} <code>{address}</code>\n"
                response_message += f"{translate('pool_info_address', user_locale)} <code>{pool}</code>\n"
                response_message += f"{translate('delegator_unclaimed_rewards', user_locale)} {unclaimed_rewards}\n"
        else:
            logger.warning(f"Failed to parse {task_type} reward data for address {address}")
            if task_type == 'validator':
                # Если нет данных для валидатора
                response_message += "\n================================\n"
                response_message += f"<b>{translate('validator_info_2', user_locale)}</b>\n"
                response_message += "================================\n"
                response_message += f"{translate('reward_address', user_locale)} <code>{address}</code>\n"
                response_message += f"{translate('staking_info_address', user_locale)} <code>{pool}</code>\n"
                response_message += f"{translate('invalid_validator_address', user_locale)}"
            elif task_type == 'delegator':
                # Если нет данных для делегатора
                response_message += "\n================================\n"
                response_message += f"<b>{translate('delegator_info', user_locale)}</b>\n"
                response_message += "================================\n"
                response_message += f"{translate('reward_address', user_locale)} <code>{address}</code>\n"
                response_message += f"{translate('pool_info_address', user_locale)} <code>{pool}</code>\n"
                response_message += f"{translate('invalid_delegator_address', user_locale)}"

    # Отправляем пользователю сообщение
    if response_message:
        logger.info(f"Successfully sent reward info to user {message.from_user.id}")
        await message.reply(response_message.strip(), parse_mode="HTML")
    else:
        logger.warning(f"No reward info to send to user {message.from_user.id}")
        await message.reply(translate("no_rewards_info", user_locale), parse_mode="HTML")
