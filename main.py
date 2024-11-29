import sys

import asyncio
import concurrent.futures
from aiogram.filters import Command
from bot import handlers

from data.tg_bot import dp, bot
from bot.middlewares import LocaleMiddleware
from db_api.database import initialize_db
from utils.create_files import create_files
from utils.filters import AdminReplyFilter, TextFilter, UserReplyToAdminFilter
from data.languages import translate, possible_prefixes
from tasks.strk_notification import send_strk_notification


async def register_handlers():
    # проверка пользователя
    dp.update.middleware(LocaleMiddleware(supported_locales=possible_prefixes, default_locale="en"))
    
    # чистим state
    dp.message.register(handlers.cancel_operation, TextFilter(text=[translate("cancel", locale) for locale in possible_prefixes]))

    # инфо команды
    dp.message.register(handlers.send_welcome, Command(commands=["start"]))
    dp.message.register(handlers.help_command, Command(commands=["help"]))
    
    # выбираем язык
    dp.message.register(handlers.choose_language, Command(commands=["language"]))
    dp.message.register(handlers.set_language, handlers.LanguageState.choosing)
    
    # получаем информацию про валидатора
    dp.message.register(handlers.get_validator_info, Command(commands=["get_validator_info"]))
    dp.message.register(handlers.handle_validator_address, handlers.ValidatorState.awaiting_address)

    # общение
    dp.message.register(handlers.start_contact_admin, Command(commands=["contact_admin"]))
    dp.message.register(handlers.send_message_to_admin, handlers.ContactAdminState.awaiting_message)
    dp.message.register(handlers.admin_reply_handler, AdminReplyFilter())
    dp.message.register(handlers.reply_handler, UserReplyToAdminFilter())

    # добавляем информацию валидатор/делегатор
    dp.message.register(handlers.add_info, Command(commands=["add_info"]))
    dp.message.register(handlers.process_add_type, handlers.AddInfoState.choose_type)
    dp.message.register(handlers.process_validator_address, handlers.AddInfoState.awaiting_validator_address)
    dp.message.register(handlers.process_delegator_address, handlers.AddInfoState.awaiting_delegate_address)
    dp.message.register(handlers.process_pool_address, handlers.AddInfoState.awaiting_pool_address)
    dp.message.register(handlers.confirm_tracking_data, handlers.AddInfoState.awaiting_prepere_confirmation)
    dp.message.register(handlers.process_confirmation, handlers.AddInfoState.awaiting_confirmation)
    
    # удаляем информацию валидатор/делегатор
    dp.message.register(handlers.start_delete_info, Command(commands=["delete_info"]))
    dp.message.register(handlers.process_delete_choice, handlers.DeleteInfoState.choose_delete_type)
    dp.message.register(handlers.delete_specific_address, handlers.DeleteInfoState.awaiting_selection)

    # cчитывай информацию валидатора/делегатора
    dp.message.register(handlers.get_tracking_full_info, Command(commands=["get_full_info"]))
    dp.message.register(handlers.get_tracking_reward_info, Command(commands=["get_reward_info"]))

    # блокировка пользователя 
    dp.message.register(handlers.start_block_user, Command('ban_user'))
    dp.message.register(handlers.process_ban, handlers.UserBlockingState.waiting_ban_info)
    dp.message.register(handlers.confirm_ban_user, handlers.UserBlockingState.confirm_operation)

    # разблокировка пользователя 
    dp.message.register(handlers.start_unblock_user, Command('unban_user'))
    dp.message.register(handlers.process_unban, handlers.UserUnblockingState.waiting_unban_info)
    dp.message.register(handlers.confirm_unban_user, handlers.UserUnblockingState.confirm_unban_operation)

    # установка / удаление ping reward msg
    dp.message.register(handlers.start_set_threshold, Command('set_reward_notification'))
    dp.message.register(handlers.set_claim_threshold, handlers.RewardClaimState.waiting_for_threshold)
    dp.message.register(handlers.clear_claim_threshold, Command('disable_reward_notification'))
    dp.message.register(handlers.show_claim_treshold_info, Command('show_reward_notification'))

    # неизвестное сообщение
    dp.message.register(handlers.unknown_command)


async def start_bot():
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        sys.exit(1)


def run_in_thread(loop, func):
    """Запуск асинхронной функции в отдельном потоке"""
    asyncio.set_event_loop(loop)
    loop.run_until_complete(func())


async def main():
    create_files()
    await initialize_db()
    await register_handlers()

    # Создаём задачи для асинхронных операций
    polling_task = asyncio.create_task(start_bot())
    notification_task = asyncio.create_task(send_strk_notification())
    
    # Ждем завершения обеих задач
    await asyncio.gather(polling_task, notification_task)

    loop = asyncio.get_event_loop()

    # Создаем пул потоков для запуска задач в отдельных потоках
    with concurrent.futures.ThreadPoolExecutor() as executor:
        # Запуск задач в разные потоки
        await asyncio.gather(
            loop.run_in_executor(executor, run_in_thread, loop, start_bot),
            loop.run_in_executor(executor, run_in_thread, loop, send_strk_notification)
        )

if __name__ == "__main__":
    asyncio.run(main())