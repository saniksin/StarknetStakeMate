from typing import List, Optional
from datetime import datetime, timezone


from sqlalchemy.future import select
from db_api import sqlalchemy_
from db_api.models import Users, Base
from data.all_paths import USERS_DB
from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession


db = sqlalchemy_.DB(f'sqlite+aiosqlite:///{USERS_DB}', pool_recycle=3600, connect_args={'check_same_thread': False})


async def get_account(user_id: str) -> Optional[Users]:
    return await db.one(Users, Users.user_id == user_id)


async def get_account_by_username(username: str) -> Optional[Users]:
    return await db.one(Users, Users.user_name == username)


async def get_user_tracking(user_id: str) -> Optional[dict]:
    # Получаем пользователя из базы данных по его user_id
    user = await db.one(Users, Users.user_id == user_id)
    
    # Если пользователь найден, возвращаем tracking_data в виде словаря
    if user:
        return user.get_tracking_data()
    
    # Если данных нет, возвращаем None
    return None


async def get_strk_notification_users() -> List[Users]:
    query = select(Users).where(Users.claim_reward_msg != 0)
    return await db.all(query)


async def initialize_db():
    await db.create_tables(Base)


async def write_to_db(user: Users):
    async with AsyncSession(db.engine) as session:
        await session.merge(user)
        await session.commit()