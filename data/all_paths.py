import sys
from pathlib import Path


# Определение корневой директории
if getattr(sys, 'frozen', False):
    ROOT_DIR = Path(sys.executable).parent.absolute()
else:
    ROOT_DIR = Path(__file__).parent.parent.absolute()


# Пути для локализации
LOCALES_DIR = ROOT_DIR / "locales"
FILES_DIR = ROOT_DIR / "files"
USERS_DB = FILES_DIR / "users.db"
ABI_DIR = ROOT_DIR / "smart_contracts_abi"
LOG_DIR = ROOT_DIR / "logs"


# Создаем необходимые директории, если они не существуют
for directory in [LOCALES_DIR, FILES_DIR, ABI_DIR, LOG_DIR]:
    directory.mkdir(parents=True, exist_ok=True)