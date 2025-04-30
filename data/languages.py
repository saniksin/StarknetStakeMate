import json
import logging
from data.all_paths import LOCALES_DIR


# Настраиваем логгер
logger = logging.getLogger(__name__)


# Загрузка локалей
def load_locales():
    locales = {}
    for file in LOCALES_DIR.glob("*.json"):
        try:
            with open(file, "r", encoding="utf-8") as f:
                locales[file.stem] = json.load(f)
        except Exception as e:
            logger.error(f"Error loading locale file {file}: {e}")
    return locales

locales = load_locales()

# Перевод ключей
def translate(key, locale="en"):
    """Возвращает перевод для заданного ключа и языка."""
    try:
        return locales.get(locale, {}).get(key, key)
    except KeyError:
        logger.warning(f"Translation key '{key}' not found for locale '{locale}'.")
        return key


possible_prefixes = ["en", "ru", "ua", "zh", "ko"]

possible_language = ["english", "русский", "українська", "中文", "한국어"]