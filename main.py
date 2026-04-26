import sys
import multiprocessing
import asyncio
import logging
import signal
import atexit
from aiogram.filters import Command
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from contextlib import suppress
from aiogram.exceptions import TelegramAPIError

from data.tg_bot import dp, bot
from bot.middlewares import LocaleMiddleware, RateLimitMiddleware
from db_api.database import initialize_db
from utils.create_files import create_files
from utils.filters import AdminReplyFilter, TextFilter, UserReplyToAdminFilter
from data.languages import translate, possible_prefixes
from tasks.attestation_alerts import send_attestation_alerts
from tasks.strk_notification import send_strk_notification
from data.models import get_admins
from utils.logger import logger
from data.tg_bot import BOT_TOKEN
from tasks.request_queue import process_request_queue
from migrations import run_all as run_migrations
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

async def _warm_contracts() -> None:
    """Pre-build cached Contract instances. Each ABI parse blocks the loop
    for several seconds the first time, so we pay it during startup instead
    of on the first user request."""
    try:
        from services.attestation_service import _attestation_contract
        from services.staking_service import _staking_contract, warm_pool_abi

        _staking_contract()
        _attestation_contract()
        warm_pool_abi()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"worker contract warm-up skipped: {exc}")


def run_queue_processor():
    """Запуск обработчика очереди в отдельном процессе"""
    try:
        # Устанавливаем имя процесса
        proc = multiprocessing.current_process()
        proc.name = "strk_bot_parsing"

        # Устанавливаем свой обработчик сигналов
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        async def _runner():
            await _warm_contracts()
            await process_request_queue()

        asyncio.run(_runner())
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

        async def _runner():
            await _warm_contracts()
            # Two independent watchers in one process — they share the warm
            # ABI cache and the price service, but tick at very different
            # frequencies (rewards: hourly, attestation: 60s).
            await asyncio.gather(
                send_strk_notification(),
                send_attestation_alerts(),
            )

        asyncio.run(_runner())
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
    dp.message.register(handlers.process_staker_address, handlers.AddInfoState.awaiting_staker_address)
    dp.message.register(handlers.process_label, handlers.AddInfoState.awaiting_label)
    dp.message.register(handlers.confirm_tracking_data, handlers.AddInfoState.awaiting_prepere_confirmation)
    dp.message.register(handlers.process_confirmation, handlers.AddInfoState.awaiting_confirmation)
    
    # удаляем информацию валидатор/делегатор
    dp.message.register(handlers.start_delete_info, TextFilter(
        text=[translate("delete_info", locale) for locale in possible_prefixes])
    )
    dp.message.register(handlers.process_delete_choice, handlers.DeleteInfoState.choose_delete_type)
    dp.message.register(handlers.delete_specific_address, handlers.DeleteInfoState.awaiting_selection)
    dp.message.register(handlers.confirm_delete_all, handlers.DeleteInfoState.confirm_delete_all)
    dp.message.register(handlers.confirm_delete_specific, handlers.DeleteInfoState.confirm_delete_specific)

    # cчитывай информацию валидатора/делегатора
    dp.message.register(handlers.get_tracking_full_info, TextFilter(
        text=[translate("get_full_info", locale) for locale in possible_prefixes])
        )
    dp.message.register(handlers.get_tracking_reward_info, TextFilter(
        text=[translate("get_reward_info", locale) for locale in possible_prefixes])
        )

    # /dashboard — compact multi-entry summary with inline buttons
    dp.message.register(handlers.dashboard_command, Command("dashboard"))

    # /rename — change an existing label
    dp.message.register(handlers.start_rename, Command("rename"))
    dp.message.register(handlers.process_rename_selection, handlers.RenameState.awaiting_selection)
    dp.message.register(handlers.process_new_label, handlers.RenameState.awaiting_new_label)

    # inline callback buttons on cards
    dp.callback_query.register(handlers.on_card_callback, lambda c: c.data and c.data.startswith("card:"))
    dp.callback_query.register(handlers.on_menu_dashboard_callback, lambda c: c.data == "menu:dashboard")

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

    # установка / удаление порогов уведомлений (Bug 4: USD + per-token)
    dp.message.register(handlers.start_set_usd_threshold, TextFilter(
        text=[translate("set_usd_threshold", locale) for locale in possible_prefixes])
    )
    dp.message.register(handlers.set_usd_threshold, handlers.RewardClaimState.waiting_for_usd)

    dp.message.register(handlers.start_set_token_threshold, TextFilter(
        text=[translate("set_token_threshold", locale) for locale in possible_prefixes])
    )
    dp.message.register(handlers.set_token_threshold, handlers.RewardClaimState.waiting_for_token)

    # Operator wallet low-balance alert (Mini App + bot share the same field
    # in notification_config; this lets bot-only users configure it too).
    dp.message.register(handlers.start_set_operator_balance, TextFilter(
        text=[translate("set_operator_balance_threshold", locale) for locale in possible_prefixes])
    )
    dp.message.register(handlers.set_operator_balance, handlers.RewardClaimState.waiting_for_op_balance)

    # Bug 5: attestation alerts. Parent-menu button caption embeds an
    # "X/Y" summary, so we accept the bare prefix or the prefix with any
    # ``: <num>/<num>`` suffix. Catching the prefix as a startswith filter
    # is impractical, so we register a custom function-filter on the parent.
    def _is_attestation_button(message: types.Message) -> bool:
        text = (message.text or "").strip()
        for loc in possible_prefixes:
            prefix = translate("attestation_toggle", loc)
            if text == prefix or text.startswith(f"{prefix}:"):
                return True
        return False

    dp.message.register(
        handlers.open_attestation_submenu,
        _is_attestation_button,
    )
    # Per-validator submenu — every tap inside lands here.
    dp.message.register(
        handlers.handle_attestation_submenu,
        handlers.AttestationMenuState.picking,
    )

    # legacy STRK threshold entry — kept under the old "set_strk_reward_notification"
    # button so users with stale UI still hit a working flow.
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

        # Pre-build the staking/attestation Contract objects. starknet-py's
        # Contract constructor parses the entire ABI synchronously (5+ seconds
        # per contract for our hand-written cairo interfaces); paying that
        # cost here keeps the very first user request snappy.
        try:
            from services.attestation_service import _attestation_contract
            from services.staking_service import _staking_contract

            _staking_contract()
            _attestation_contract()
            logger.info("Contract ABIs warmed up")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"contract warm-up skipped: {exc}")

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
        await run_migrations()
        
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

def run() -> None:
    """Sync entry point used by ``uv run stakemate-bot``."""
    signal.signal(signal.SIGINT, handle_signals)
    signal.signal(signal.SIGTERM, handle_signals)
    try:
        multiprocessing.freeze_support()
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program terminated by user")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Unexpected error: {exc}")


if __name__ == "__main__":
    run()