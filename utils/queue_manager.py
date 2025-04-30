from collections import deque
from typing import Dict, Deque, List, Tuple, Callable, Any
import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
from collections import defaultdict
import json
from db_api.database import Users, db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

logger = logging.getLogger(__name__)

@dataclass
class QueueItem:
    user_id: int
    request_type: str
    timestamp: datetime
    task_data: dict

class QueueManager:
    def __init__(self):
        # Словарь для хранения очередей
        self.queues = defaultdict(list)
        # Словарь для хранения статусов обработки
        self.processing = defaultdict(set)
        self.max_queue_size = 100  # Максимальный размер очереди
        self.max_concurrent = 5    # Максимальное количество одновременных запросов
        self.semaphores: Dict[str, asyncio.Semaphore] = {}

    async def add_to_queue(self, queue_name: str, user_id: int, task_data: dict) -> Tuple[int, bool]:
        """
        Добавляет задачу в очередь.
        Возвращает позицию в очереди и флаг успеха.
        """
        # Проверяем, не находится ли пользователь уже в очереди
        if user_id in {task[0] for task in self.queues[queue_name]}:
            return 0, False

        # Добавляем в очередь
        self.queues[queue_name].append((user_id, task_data))
        position = len(self.queues[queue_name])

        return position, True

    async def process_queue(self, queue_name: str, process_func: Callable[[int, dict], Any]):
        """
        Обрабатывает очередь, вызывая process_func для каждой задачи.
        """
        while True:
            if self.queues[queue_name]:
                # Получаем первую задачу из очереди
                user_id, task_data = self.queues[queue_name][0]

                try:
                    # Добавляем пользователя в множество обрабатываемых
                    self.processing[queue_name].add(user_id)

                    # Обрабатываем задачу
                    await process_func(user_id, task_data)

                except Exception as e:
                    print(f"Error processing task for user {user_id}: {e}")

                finally:
                    # Удаляем задачу из очереди и из множества обрабатываемых
                    self.queues[queue_name].pop(0)
                    self.processing[queue_name].discard(user_id)

            await asyncio.sleep(1)  # Небольшая пауза между проверками очереди

    def get_queue_position(self, queue_name: str, user_id: int) -> int:
        """
        Возвращает позицию пользователя в очереди
        """
        if queue_name not in self.queues:
            return -1

        for i, item in enumerate(self.queues[queue_name]):
            if item.user_id == user_id:
                return i + 1
        return -1

    def is_processing(self, queue_name: str, user_id: int) -> bool:
        """
        Проверяет, обрабатывается ли запрос пользователя
        """
        if queue_name not in self.processing:
            return False
        return any(item.user_id == user_id for item in self.processing[queue_name])

queue_manager = QueueManager() 