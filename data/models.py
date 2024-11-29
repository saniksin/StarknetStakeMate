import os
import asyncio

class AutoRepr:
    """Добавляет поддержку repr для моделей."""
    def __repr__(self) -> str:
        values = ('{}={!r}'.format(key, value) for key, value in vars(self).items())
        return '{}({})'.format(self.__class__.__name__, ', '.join(values))


def get_admins():
    """
    Получение списка ID администраторов из .env
    """
    return [int(admin_id) for admin_id in os.getenv("ADMINS_ID", "").split(",") if admin_id]


value_of_one_monent_notification = 50
semaphore = asyncio.Semaphore(value_of_one_monent_notification)