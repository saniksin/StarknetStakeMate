"""User lookup/registration with concise logging.

Previously this module printed ``new user: …`` on every message, spamming
logs with already-known users. Now we log only at the two moments that
actually matter: INSERT (first contact) and username UPDATE.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from db_api.database import db, get_account, write_to_db
from db_api.models import Users
from utils.logger import logger


async def get_or_create_user(user_id, user_name, user_language, registration_date) -> Users:
    user = await get_account(user_id)
    if user is None:
        async with AsyncSession(db.engine) as session:
            user = Users(
                user_id=user_id,
                user_name=user_name,
                user_language=user_language,
                registration_data=registration_date,
            )
            await session.merge(user)
            await session.commit()
        logger.info(f"new user: {user_id} @{user_name} ({user_language})")
        return user

    if user_name != user.user_name:
        old = user.user_name
        user.user_name = user_name
        await write_to_db(user)
        logger.info(f"username changed for {user_id}: @{old} → @{user_name}")
    return user
