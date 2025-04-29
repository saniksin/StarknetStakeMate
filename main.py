import sys
import threading
import asyncio
import logging
import signal
from aiogram.filters import Command
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from contextlib import suppress
from aiogram.exceptions import TelegramAPIError

from data.tg_bot import dp, bot
from bot.middlewares import LocaleMiddleware, RateLimitMiddleware
from db_api.database import initialize_db
from utils.create_files import create_files
from utils.filters import AdminReplyFilter, TextFilter, UserReplyToAdminFilter
from data.languages import translate, possible_prefixes
from tasks.strk_notification import send_strk_notification
from data.models import get_admins
from utils.logger import logger
from data.tg_bot import BOT_TOKEN
from tasks.request_queue import process_request_queue
from migrate_queue import migrate
from bot import handlers

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Флаг для отслеживания состояния завершения
is_shutting_down = False

async def shutdown(signal_type=None):
    """
    Корректное завершение работы бота и всех фоновых задач
    """
    global is_shutting_down
    
    # Проверяем, не выполняется ли уже завершение работы
    if is_shutting_down:
        return
        
    is_shutting_down = True
    
    logger.info(f'Получен сигнал завершения работы: {signal_type}')
    
    try:
        # Останавливаем поллинг
        logger.info('Останавливаем поллинг...')
        with suppress(Exception):
            await dp.stop_polling()
        
        # Отменяем все задачи
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        logger.info(f'Отменяем {len(tasks)} задач...')
        
        # Отменяем все задачи
        for task in tasks:
            with suppress(asyncio.CancelledError):
                task.cancel()
            
        # Ждем завершения всех задач с таймаутом
        logger.info('Ожидаем завершения задач...')
        with suppress(asyncio.TimeoutError):
            await asyncio.wait(tasks, timeout=3)
        
        # Закрываем сессию бота
        logger.info('Закрываем соединения...')
        with suppress(Exception):
            await bot.session.close()
        
        logger.info('Завершение работы успешно выполнено')
        
    except Exception as e:
        logger.error(f'Ошибка при завершении работы: {e}')
    finally:
        # Принудительно завершаем программу
        sys.exit(0)

def handle_signals(signum, frame):
    """Обработчик сигналов"""
    if not is_shutting_down:  # Проверяем, не выполняется ли уже завершение
        logger.info(f'Получен сигнал: {signal.Signals(signum).name}')
        # Запускаем завершение работы в новой задаче
        loop = asyncio.get_event_loop()
        loop.create_task(shutdown(signal.Signals(signum).name))

# Регистрируем обработчики сигналов
signal.signal(signal.SIGINT, handle_signals)
signal.signal(signal.SIGTERM, handle_signals)

async def register_handlers():
    # проверка пользователя
    dp.update.middleware(LocaleMiddleware(supported_locales=possible_prefixes, default_locale="en"))
    dp.update.middleware(RateLimitMiddleware())
    
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
        logger.info("Starting bot initialization...")
        await initialize_db()
        logger.info("Database initialized successfully")
        
        # Запускаем бота
        await dp.start_polling(bot)
        
    except Exception as e:
        logger.error(f"Error during bot startup: {e}")
    finally:
        if not is_shutting_down:  # Проверяем, не выполняется ли уже завершение
            await shutdown()


async def main():
    logger.info("Starting application...")
    try:
        # Выполняем предварительные задачи
        create_files()
        await initialize_db()
        await register_handlers()
        await migrate()
        
        # Запускаем фоновые задачи
        tasks = [
            asyncio.create_task(send_strk_notification()),
            asyncio.create_task(process_request_queue()),
            asyncio.create_task(start_bot())
        ]
        
        # Ждем завершения любой из задач или сигнала завершения
        done, pending = await asyncio.wait(
            tasks, 
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Отменяем оставшиеся задачи
        for task in pending:
            task.cancel()
        
    except Exception as e:
        logger.error(f"Unexpected error in main: {e}")
    finally:
        if not is_shutting_down:  # Проверяем, не выполняется ли уже завершение
            await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program terminated by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")