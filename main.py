import sys
import multiprocessing
import asyncio
import logging
import signal
import atexit
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
is_shutting_down = multiprocessing.Value('b', False)

# Список для хранения процессов
background_processes = []

def cleanup_processes():
    """Функция очистки процессов при выходе"""
    global background_processes
    if background_processes:
        for process in background_processes:
            if process and process.is_alive():
                process.terminate()
                process.join(timeout=1)

# Регистрируем функцию очистки
atexit.register(cleanup_processes)

def run_queue_processor():
    """Запуск обработчика очереди в отдельном процессе"""
    try:
        # Устанавливаем имя процесса
        proc = multiprocessing.current_process()
        proc.name = "strk_bot_parsing"
        
        # Устанавливаем свой обработчик сигналов
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        
        asyncio.run(process_request_queue())
    except Exception as e:
        logger.error(f"Error in queue processor: {e}")

def run_notification_processor():
    """Запуск обработчика уведомлений в отдельном процессе"""
    try:
        # Устанавливаем имя процесса
        proc = multiprocessing.current_process()
        proc.name = "strk_bot_notification"
        
        # Устанавливаем свой обработчик сигналов
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        
        asyncio.run(send_strk_notification())
    except Exception as e:
        logger.error(f"Error in notification processor: {e}")

def kill_background_processes():
    """Завершает все фоновые процессы"""
    global background_processes
    
    for process in background_processes:
        try:
            if process and process.is_alive():
                logger.info(f"Terminating process {process.name}")
                process.terminate()
                process.join(timeout=2)
                
                if process.is_alive():
                    logger.warning(f"Process {process.name} did not terminate gracefully, killing it")
                    process.kill()
                    process.join(timeout=1)
        except Exception as e:
            logger.error(f"Error while terminating process: {e}")
    
    # Очищаем список процессов
    background_processes.clear()

async def shutdown(signal_type=None):
    """
    Корректное завершение работы бота и всех фоновых задач
    """
    with is_shutting_down.get_lock():
        if is_shutting_down.value:
            return
        is_shutting_down.value = True
    
    logger.info(f'Получен сигнал завершения работы: {signal_type}')
    
    try:
        # Останавливаем поллинг
        logger.info('Останавливаем поллинг...')
        try:
            await dp.stop_polling()
        except Exception as e:
            logger.error(f"Error stopping polling: {e}")
        
        # Закрываем сессию бота
        logger.info('Закрываем соединения...')
        try:
            await bot.session.close()
        except Exception as e:
            logger.error(f"Error closing bot session: {e}")
        
        # Завершаем фоновые процессы
        logger.info('Завершаем фоновые процессы...')
        kill_background_processes()
        
        logger.info('Завершение работы успешно выполнено')
        
    except Exception as e:
        logger.error(f'Ошибка при завершении работы: {e}')
    finally:
        sys.exit(0)

def handle_signals(signum, frame):
    """Обработчик сигналов"""
    with is_shutting_down.get_lock():
        if is_shutting_down.value:
            return
            
    signal_name = signal.Signals(signum).name
    logger.info(f'Получен сигнал: {signal_name}')
    
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(shutdown(signal_name))
    except Exception as e:
        logger.error(f"Error in signal handler: {e}")
        sys.exit(1)

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
        if not is_shutting_down.value:
            await shutdown()

async def main():
    logger.info("Starting application...")
    try:
        # Выполняем предварительные задачи
        create_files()
        await initialize_db()
        await register_handlers()
        await migrate()
        
        # Запускаем процессы для фоновых задач
        queue_process = multiprocessing.Process(target=run_queue_processor)
        notification_process = multiprocessing.Process(target=run_notification_processor)
        
        queue_process.daemon = True
        notification_process.daemon = True
        
        # Добавляем процессы в список для отслеживания
        background_processes.extend([queue_process, notification_process])
        
        # Запускаем процессы
        for process in background_processes:
            process.start()
        
        # Запускаем основной процесс бота
        await start_bot()
        
    except Exception as e:
        logger.error(f"Unexpected error in main: {e}")
    finally:
        if not is_shutting_down.value:
            await shutdown()

if __name__ == "__main__":
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGINT, handle_signals)
    signal.signal(signal.SIGTERM, handle_signals)
    
    try:
        # Для Windows нужно защитить точку входа
        multiprocessing.freeze_support()
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program terminated by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)