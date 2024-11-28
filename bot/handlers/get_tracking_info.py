import json
import asyncio

from aiogram import types
from aiogram.fsm.context import FSMContext

from data.languages import translate
from db_api.models import Users
from data.contracts import Contracts
from parse.parse_info import parse_delegator_staking_info, parse_validator_staking_info
from parse.msg_format import parse_delegator_info, parse_validator_info


# Хендлер команды /get_info
async def get_tracking_data(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    # Загрузка данных отслеживания пользователя
    if user_object.tracking_data:
        tracking_data = json.loads(user_object.tracking_data)
        print(tracking_data)
    else:
        # msg data is empty
        await message.reply(translate("tracking_data_empty", user_locale), parse_mode="HTML")
        return
    
    if len(tracking_data['data_pair']) == 0:
        await message.reply(translate("no_addresses_to_parse", user_locale), parse_mode="HTML")
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
            async_tasks.append(asyncio.create_task(parse_validator_staking_info(address)))
            task_info.append((index, 'validator', address, pool))  # Сохраняем для отслеживания
        else:
            # Это делегатор
            async_tasks.append(asyncio.create_task(parse_delegator_staking_info(address, pool)))
            task_info.append((index, 'delegator', address, pool))  # Сохраняем для отслеживания

    # Ожидание завершения всех задач
    results = await asyncio.gather(*async_tasks)

    # Формирование сообщения с результатами
    response_message = ""

    # Прочитаем результаты в том порядке, в котором они были запущены
    for index, task_result in zip([t[0] for t in task_info], results):
        task_type, address, pool = task_info[index][1], task_info[index][2], task_info[index][3]

        if task_result:
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
        await message.reply(response_message, parse_mode="HTML")
    else:
        await message.reply(translate("no_tracking_info", user_locale), parse_mode="HTML")


def format_section(user_locale, task_type, task_result, address, pool, info_address_key, pool_address_key, no_data=False):
    """
    Функция для форматирования секции информации (делегатор или валидатор) с адресами и пулами.
    """
    # Формируем разделитель с отступами
    separator = "\n================================\n"

    # Заголовок в зависимости от типа задачи
    if task_type == 'validator':
        section_title = translate('validator_info_2', user_locale)
        info_address = translate(info_address_key, user_locale)
        pool_address = translate(pool_address_key, user_locale)
    else:
        section_title = translate('delegator_info', user_locale)
        info_address = translate(info_address_key, user_locale)
        pool_address = translate(pool_address_key, user_locale)

    # Формируем контент секции
    if no_data:
        section_content = f"{separator}<b>{section_title}</b>{separator}{translate('no_data_for_' + task_type, user_locale)} {address} | {pool}\n"
    else:
        section_content = f"{separator}<b>{section_title}</b>{separator}{info_address} <code>{address}</code>\n{pool_address} <code>{pool}</code>\n"
        # Дополнительные данные (передаем для валидатора или делегатора)
        if task_result:
            section_content += parse_delegator_info(task_result, user_locale) if task_type == 'delegator' else parse_validator_info(task_result, user_locale, False)

    return section_content
