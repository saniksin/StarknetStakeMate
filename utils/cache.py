from aiocache import Cache
from aiocache.serializers import JsonSerializer
from data.all_paths import FILES_DIR

# Создаем директорию для кеша, если она не существует
CACHE_DIR = FILES_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Конфигурация кеша
cache = Cache(
    Cache.MEMORY,  # Используем память для кеша
    serializer=JsonSerializer(),  # Сериализуем данные в JSON
    namespace="starknet_bot",  # Пространство имен для кеша
    ttl=300  # Время жизни кеша - 5 минут
)

# Функция для создания ключа кеша
def get_cache_key(user_id: int, command: str) -> str:
    return f"{user_id}_{command}"

# Функция для очистки кеша пользователя
async def clear_user_cache(user_id: int):
    """Очищает кеш для конкретного пользователя"""
    keys_to_delete = [
        get_cache_key(user_id, "full_info"),
        get_cache_key(user_id, "reward_info")
    ]
    for key in keys_to_delete:
        await cache.delete(key) 