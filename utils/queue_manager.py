from collections import deque
from typing import Dict, Deque, List, Tuple, Callable, Any
import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

@dataclass
class QueueItem:
    user_id: int
    request_type: str
    timestamp: datetime
    task_data: dict

class QueueManager:
    def __init__(self):
        self.queues: Dict[str, Deque[QueueItem]] = {}
        self.processing: Dict[str, List[QueueItem]] = {}
        self.queue_locks: Dict[str, asyncio.Lock] = {}
        self.max_queue_size = 100  # Максимальный размер очереди
        self.max_concurrent = 5    # Максимальное количество одновременных запросов
        self.semaphores: Dict[str, asyncio.Semaphore] = {}

    async def add_to_queue(self, queue_name: str, user_id: int, task_data: dict) -> Tuple[int, bool]:
        """
        Добавляет задачу в очередь и возвращает позицию в очереди
        """
        if queue_name not in self.queues:
            self.queues[queue_name] = deque()
            self.processing[queue_name] = []
            self.queue_locks[queue_name] = asyncio.Lock()
            self.semaphores[queue_name] = asyncio.Semaphore(self.max_concurrent)

        if len(self.queues[queue_name]) >= self.max_queue_size:
            return -1, False

        queue_item = QueueItem(
            user_id=user_id,
            request_type=queue_name,
            timestamp=datetime.now(),
            task_data=task_data
        )
        
        position = len(self.queues[queue_name]) + 1
        self.queues[queue_name].append(queue_item)
        return position, True

    async def process_queue(self, queue_name: str, process_func: Callable[[int, dict], Any]):
        """
        Обрабатывает очередь задач
        """
        async with self.queue_locks[queue_name]:
            if not self.queues[queue_name]:
                return

        while True:
            async with self.queue_locks[queue_name]:
                if not self.queues[queue_name]:
                    break

                # Проверяем, есть ли свободные слоты для обработки
                if len(self.processing[queue_name]) >= self.max_concurrent:
                    break

                # Берем следующую задачу из очереди
                item = self.queues[queue_name].popleft()
                self.processing[queue_name].append(item)

            # Обрабатываем задачу с учетом семафора
            async with self.semaphores[queue_name]:
                try:
                    await process_func(item.user_id, item.task_data)
                except Exception as e:
                    logger.error(f"Error processing task for user {item.user_id}: {e}")
                finally:
                    async with self.queue_locks[queue_name]:
                        if item in self.processing[queue_name]:
                            self.processing[queue_name].remove(item)

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