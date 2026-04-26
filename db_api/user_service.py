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
    """Idempotent user upsert.

    Two parallel ``/start`` handlers used to race here: both saw
    ``get_account → None`` and both did ``session.merge(Users(...))``,
    creating two rows with the same ``user_id`` (the PK is autoincrement
    ``id``). Once that happened, every ``scalar_one_or_none()`` on that
    user crashed with ``MultipleResultsFound`` and tipped the whole
    notification loop over.

    The fix is two-pronged:
      1. ``initialize_db`` installs a UNIQUE INDEX on ``users.user_id``
         so the second concurrent INSERT now fails at the DB level.
      2. We catch the ``IntegrityError`` here, re-fetch the row that
         won the race, and return it. The losing call still gets a
         valid Users object; the user is none the wiser.
    """
    from sqlalchemy.exc import IntegrityError

    user = await get_account(user_id)
    if user is None:
        async with AsyncSession(db.engine) as session:
            user = Users(
                user_id=user_id,
                user_name=user_name,
                user_language=user_language,
                registration_data=registration_date,
            )
            try:
                session.add(user)
                await session.commit()
                logger.info(f"new user: {user_id} @{user_name} ({user_language})")
                return user
            except IntegrityError:
                # Lost the race against another /start; the row is there now.
                await session.rollback()
        existing = await get_account(user_id)
        if existing is not None:
            return existing
        # Extremely unlikely: integrity error but no row found. Surface it.
        raise

    if user_name != user.user_name:
        old = user.user_name
        user.user_name = user_name
        await write_to_db(user)
        logger.info(f"username changed for {user_id}: @{old} → @{user_name}")
    return user
