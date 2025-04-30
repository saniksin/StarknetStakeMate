from multiprocessing import Manager
from datetime import datetime, timedelta
from typing import Any, Optional
from data.all_paths import FILES_DIR

# Создаем директорию для кеша, если она не существует
CACHE_DIR = FILES_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Создаем общий словарь для всех процессов
manager = Manager()
_cache = manager.dict()

class SharedCache:
    def __init__(self, ttl: int = 300):
        self.ttl = ttl

    async def get(self, key: str) -> Optional[Any]:
        if key not in _cache:
            return None
        value, expiry = _cache[key]
        if datetime.now() > expiry:
            await self.delete(key)
            return None
        return value

    async def set(self, key: str, value: Any) -> None:
        expiry = datetime.now() + timedelta(seconds=self.ttl)
        _cache[key] = (value, expiry)

    async def delete(self, key: str) -> None:
        if key in _cache:
            del _cache[key]

# Создаем экземпляр кеша
cache = SharedCache(ttl=300)

# Функция для создания ключа кеша
def get_cache_key(user_id: int, command: str) -> str:
    return f"{user_id}_{command}"

# Функция для очистки кеша пользователя
async def clear_user_cache(user_id: int):
    """Очищает кеш для конкретного пользователя"""
    keys_to_delete = [
        get_cache_key(user_id, "full_info"),
        get_cache_key(user_id, "reward_info"),
        get_cache_key(user_id, "validator_info")
    ]
    for key in keys_to_delete:
        await cache.delete(key) 