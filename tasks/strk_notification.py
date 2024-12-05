import asyncio
import json

from db_api.database import get_strk_notification_users, write_to_db
from db_api.models import Users
from data.tg_bot import bot
from data.languages import translate
from data.models import semaphore
from parse.parse_info import parse_validator_staking_info, parse_delegator_staking_info
from utils.format_decimal import format_decimal
from data.contracts import Contracts


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

                
                # strk_notification_msg - Уведомление о достижение минимального порога для клейма реварда токенов STRK:
                # claim_for_validator - Доступный клейм для валидатора: {amount_1} STRK >= {amount_2} 
                # claim_for_delegator - Доступный клейм для делагатора: {amount_1} STRK >= {amount_2} 

                if result_type == 'validator':
                    unclaimed_rewards_own = format_decimal(task_result[0]['unclaimed_rewards_own'])
                    if float(unclaimed_rewards_own) >= user.claim_reward_msg:

                        if message_welcome not in response_message:
                            response_message += message_welcome

                        # Если клейм для валидатора превышает порог
                        response_message += "\n================================\n"
                        response_message += f"{translate('validator_info_2', user.user_language)}\n"
                        response_message += "================================\n"
                        response_message += f"{translate('reward_address', user.user_language)} <code>{address}</code>\n"
                        response_message += f"{translate('staking_info_address', user.user_language)} <code>{pool}</code>\n"
                        response_message += f"{translate('claim_for_validator', user.user_language).format(
                            amount_1=unclaimed_rewards_own)}"

                elif result_type == 'delegator':
                    unclaimed_rewards = format_decimal(task_result[0]['unclaimed_rewards'])
                    if float(unclaimed_rewards) >= user.claim_reward_msg:
                        # Если клейм для делегатора превышает порог
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
            await bot.send_message(chat_id=user.user_id, text=response_message, parse_mode="HTML")
        # else:
        #     await bot.send_message(chat_id=user.user_id, text=translate("no_rewards_to_claim", user.user_language), parse_mode="HTML")

        # Обновляем информацию о клейме в базе данных
        await write_to_db(user)


async def send_strk_notification():
    while True:
        
        result_users = await get_strk_notification_users()
        final_user_list = []
        if result_users:
            for user in result_users:
                tracking_data = json.loads(user.tracking_data)

                if len(tracking_data['data_pair']) == 0:
                    user.claim_reward_msg = 0
                    await write_to_db(user)
                    await bot.send_message(chat_id=user.user_id, text=translate("no_addresses_to_parse_info", user.user_language), parse_mode="HTML")
                else:
                    final_user_list.append(user)

        print(f'Длинна списка с уведомлениями - {len(final_user_list)}')
        notification_task = []
        if final_user_list:
            for user in final_user_list:
                notification_task.append(asyncio.create_task(start_parse_and_send_notification(user)))
            
            await asyncio.gather(*notification_task)
        
        await asyncio.sleep(3600)
