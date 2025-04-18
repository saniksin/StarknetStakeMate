from collections import deque
from typing import Dict, Deque, List, Tuple
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
    task: asyncio.Task

class RequestQueue:
    def __init__(self):
        self.queue: Deque[QueueItem] = deque()
        self.processing: Dict[int, QueueItem] = {}
        self.max_concurrent = 300  # Максимальное количество одновременных запросов
        
    async def add_request(self, user_id: int, request_type: str, task: asyncio.Task) -> int:
        """Добавляет запрос в очередь и возвращает позицию в очереди"""
        queue_item = QueueItem(
            user_id=user_id,
            request_type=request_type,
            timestamp=datetime.now(),
            task=task
        )
        self.queue.append(queue_item)
        return len(self.queue)
        
    async def process_next(self):
        """Обрабатывает следующий запрос из очереди, если есть свободные слоты"""
        while len(self.processing) < self.max_concurrent and self.queue:
            item = self.queue.popleft()
            self.processing[item.user_id] = item
            asyncio.create_task(self._process_item(item))
            
    async def _process_item(self, item: QueueItem):
        """Обрабатывает один запрос"""
        try:
            await item.task
        finally:
            self.processing.pop(item.user_id, None)
            await self.process_next()
            
    def get_queue_position(self, user_id: int) -> int:
        """Возвращает позицию пользователя в очереди"""
        for i, item in enumerate(self.queue):
            if item.user_id == user_id:
                return i + 1
        return 0
        
    def is_processing(self, user_id: int) -> bool:
        """Проверяет, обрабатывается ли запрос пользователя"""
        return user_id in self.processing

# Глобальный экземпляр очереди
request_queue = RequestQueue()

class QueueManager:
    def __init__(self):
        self.queues: Dict[str, deque] = {}
        self.processing: Dict[str, bool] = {}
        self.queue_locks: Dict[str, asyncio.Lock] = {}
        self.max_queue_size = 100  # Максимальный размер очереди

    async def add_to_queue(self, queue_name: str, user_id: int, task_data: dict) -> Tuple[int, bool]:
        """
        Добавляет задачу в очередь и возвращает позицию в очереди
        """
        if queue_name not in self.queues:
            self.queues[queue_name] = deque()
            self.processing[queue_name] = False
            self.queue_locks[queue_name] = asyncio.Lock()

        if len(self.queues[queue_name]) >= self.max_queue_size:
            return -1, False

        position = len(self.queues[queue_name]) + 1
        self.queues[queue_name].append((user_id, task_data))
        return position, True

    async def process_queue(self, queue_name: str, process_func):
        """
        Обрабатывает очередь задач
        """
        async with self.queue_locks[queue_name]:
            if self.processing[queue_name] or not self.queues[queue_name]:
                return

            self.processing[queue_name] = True

        try:
            while self.queues[queue_name]:
                user_id, task_data = self.queues[queue_name][0]
                try:
                    await process_func(user_id, task_data)
                except Exception as e:
                    logger.error(f"Error processing task for user {user_id}: {e}")
                finally:
                    self.queues[queue_name].popleft()
        finally:
            self.processing[queue_name] = False

    def get_queue_position(self, queue_name: str, user_id: int) -> int:
        """
        Возвращает позицию пользователя в очереди
        """
        if queue_name not in self.queues:
            return -1

        for i, (uid, _) in enumerate(self.queues[queue_name]):
            if uid == user_id:
                return i + 1
        return -1

queue_manager = QueueManager() 