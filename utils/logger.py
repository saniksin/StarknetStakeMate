import sys
from loguru import logger
from data.all_paths import LOG_DIR

# Конфигурация логгера
logger.remove()  # Удаляем стандартный обработчик

# Добавляем обработчик для записи в файл
logger.add(
    LOG_DIR / "bot_{time}.log",
    rotation="1 day",  # Ротация логов каждый день
    retention="7 days",  # Хранение логов 7 дней
    compression="zip",  # Сжатие старых логов
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} - {message}"
)

# Добавляем обработчик для вывода в консоль
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)

# Экспортируем настроенный логгер
__all__ = ["logger"] 