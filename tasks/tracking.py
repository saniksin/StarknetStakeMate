from utils.cache import clear_user_cache
from utils.logger import logger

async def update_user_tracking_data(user_id: int, new_data: dict):
    """Обновляет данные отслеживания пользователя"""
    try:
        async with AsyncSession(db.engine) as session:
            user = await session.get(Users, user_id)
            if user:
                user.tracking_data = json.dumps(new_data)
                await session.commit()
                
                # Очищаем кеш пользователя после обновления данных
                logger.info(f"Clearing cache for user {user_id} after updating tracking data")
                await clear_user_cache(user_id)
                
                return True
    except Exception as e:
        logger.error(f"Error updating tracking data for user {user_id}: {str(e)}")
    return False 