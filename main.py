import sys
import threading

import asyncio
from aiogram.filters import Command
from bot import handlers

from data.tg_bot import dp, bot
from bot.middlewares import LocaleMiddleware
from db_api.database import initialize_db
from utils.create_files import create_files
from utils.filters import AdminReplyFilter, TextFilter, UserReplyToAdminFilter
from data.languages import translate, possible_prefixes
from tasks.strk_notification import send_strk_notification
from data.models import get_admins


async def register_handlers():
    # проверка пользователя
    dp.update.middleware(LocaleMiddleware(supported_locales=possible_prefixes, default_locale="en"))
    
    # возвращаемся в главное меню
    dp.message.register(handlers.finish_operation, TextFilter(text=[translate("cancel", locale) for locale in possible_prefixes]))

    # инфо команды
    dp.message.register(handlers.send_welcome, Command(commands=["start"]))
    dp.message.register(handlers.help_command, TextFilter(text=[translate("help", locale) for locale in possible_prefixes]))
    
    # выбираем язык
    dp.message.register(handlers.choose_language, TextFilter(text=[translate("language", locale) for locale in possible_prefixes]))
    dp.message.register(handlers.set_language, handlers.LanguageState.choosing)
    
    # получаем информацию про валидатора
    dp.message.register(handlers.get_validator_info, TextFilter(
        text=[translate("get_validator_info", locale) for locale in possible_prefixes])
    )
    dp.message.register(handlers.handle_validator_address, handlers.ValidatorState.awaiting_address)

    # общение
    dp.message.register(handlers.start_contact_admin, TextFilter(text=[translate("contact_admin", locale) for locale in possible_prefixes]))
    dp.message.register(handlers.send_message_to_admin, handlers.ContactAdminState.awaiting_message)
    dp.message.register(handlers.admin_reply_handler, AdminReplyFilter())
    dp.message.register(handlers.reply_handler, UserReplyToAdminFilter())

    # добавляем информацию валидатор/делегатор
    dp.message.register(handlers.add_info, TextFilter(
        text=[translate("add_info", locale) for locale in possible_prefixes])
    )
    dp.message.register(handlers.process_add_type, handlers.AddInfoState.choose_type)
    dp.message.register(handlers.process_validator_address, handlers.AddInfoState.awaiting_validator_address)
    dp.message.register(handlers.process_delegator_address, handlers.AddInfoState.awaiting_delegate_address)
    dp.message.register(handlers.process_pool_address, handlers.AddInfoState.awaiting_pool_address)
    dp.message.register(handlers.confirm_tracking_data, handlers.AddInfoState.awaiting_prepere_confirmation)
    dp.message.register(handlers.process_confirmation, handlers.AddInfoState.awaiting_confirmation)
    
    # удаляем информацию валидатор/делегатор
    dp.message.register(handlers.start_delete_info, TextFilter(
        text=[translate("delete_info", locale) for locale in possible_prefixes])
    )
    dp.message.register(handlers.process_delete_choice, handlers.DeleteInfoState.choose_delete_type)
    dp.message.register(handlers.delete_specific_address, handlers.DeleteInfoState.awaiting_selection)

    # cчитывай информацию валидатора/делегатора
    dp.message.register(handlers.get_tracking_full_info, TextFilter(
        text=[translate("get_full_info", locale) for locale in possible_prefixes])
        )
    dp.message.register(handlers.get_tracking_reward_info, TextFilter(
        text=[translate("get_reward_info", locale) for locale in possible_prefixes])
        )

    # блокировка пользователя 
    dp.message.register(handlers.start_block_user, Command('ban_user'))
    dp.message.register(handlers.process_ban, handlers.UserBlockingState.waiting_ban_info)
    dp.message.register(handlers.confirm_ban_user, handlers.UserBlockingState.confirm_operation)

    # разблокировка пользователя 
    dp.message.register(handlers.start_unblock_user, Command('unban_user'))
    dp.message.register(handlers.process_unban, handlers.UserUnblockingState.waiting_unban_info)
    dp.message.register(handlers.confirm_unban_user, handlers.UserUnblockingState.confirm_unban_operation)

    # установка / удаление ping reward msg
    dp.message.register(handlers.open_notification_menu, TextFilter(
        text=[translate("notifications", locale) for locale in possible_prefixes])
    )
    dp.message.register(handlers.open_strk_notification_menu, TextFilter(
        text=[translate("set_strk_notification", locale) for locale in possible_prefixes])
    )

    # установка / удаление ping strk reward msg
    dp.message.register(handlers.start_set_threshold, TextFilter(
        text=[translate("set_strk_reward_notification", locale) for locale in possible_prefixes])
    )
    dp.message.register(handlers.set_claim_threshold, handlers.RewardClaimState.waiting_for_threshold)
    dp.message.register(handlers.clear_claim_threshold, TextFilter(
        text=[translate("disable_strk_reward_notification", locale) for locale in possible_prefixes])
    )
    dp.message.register(handlers.show_claim_treshold_info, TextFilter(
        text=[translate("show_strk_reward_notification", locale) for locale in possible_prefixes])
    )

    # неизвестное сообщение
    dp.message.register(handlers.unknown_command)


async def start_bot():
    try:
        await initialize_db()
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        sys.exit(1)


def run_in_thread(func):
    """Запуск асинхронной функции в отдельном потоке."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(func())
    finally:
        loop.close()


async def main():
    # Выполняем предварительные задачи
    create_files()
    await initialize_db()
    await register_handlers()

    # Создаём поток для уведомлений
    notification_thread = threading.Thread(
        target=run_in_thread, args=(send_strk_notification,), daemon=True
    )
    notification_thread.start()

    # Запускаем бота в основном потоке
    await start_bot()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Программа завершена.")