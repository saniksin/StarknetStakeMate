from collections import defaultdict
import time
from typing import Dict, List
from data.languages import translate


class RateLimiter:
    def __init__(self, max_requests: int = 20, time_window: int = 60):
        """
        Инициализация RateLimiter
        :param max_requests: Максимальное количество запросов
        :param time_window: Временное окно в секундах
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.user_requests: Dict[int, List[float]] = defaultdict(list)
        self.warned_users = set()

    def is_allowed(self, user_id: int, locale: str = "en") -> tuple[bool, str | None]:
        """
        Проверяет, разрешен ли запрос для пользователя
        :param user_id: ID пользователя
        :param locale: Локаль пользователя
        :return: tuple[bool, str | None] - (разрешен ли запрос, сообщение об ошибке если есть)
        """
        current_time = time.time()
        
        # Очищаем старые запросы
        self.user_requests[user_id] = [
            req_time for req_time in self.user_requests[user_id]
            if current_time - req_time < self.time_window
        ]
        
        # Проверяем количество запросов
        if len(self.user_requests[user_id]) >= self.max_requests:
            if user_id not in self.warned_users:
                self.warned_users.add(user_id)
                return False, translate("rate_limit_warning", locale=locale)
            return False, None
        
        # Добавляем новый запрос
        self.user_requests[user_id].append(current_time)
        return True, None 