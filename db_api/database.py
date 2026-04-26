from typing import List, Optional
from datetime import datetime, timezone


from sqlalchemy.future import select
from db_api import sqlalchemy_
from db_api.models import Users, Base
from data.all_paths import USERS_DB
from sqlalchemy import and_, or_, update
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
    """Users that have *any* notification configured.

    Either the legacy STRK-only ``claim_reward_msg`` or the new
    ``notification_config`` JSON (USD threshold and/or per-token thresholds).
    """
    query = select(Users).where(
        (Users.claim_reward_msg != 0) | (Users.notification_config.isnot(None))
    )
    return await db.all(query)


async def initialize_db():
    await db.create_tables(Base)


async def write_to_db(user: Users):
    async with AsyncSession(db.engine) as session:
        await session.merge(user)
        await session.commit()


async def update_attestation_state(user_id: int, state: dict) -> None:
    """Atomically refresh ``notification_config["_attestation_state"]``.

    The notifier loop holds a stale Users snapshot for the whole cycle
    (RPC + Telegram = many seconds). A blanket ``write_to_db(user)`` would
    ``merge()`` every column and clobber concurrent edits — most painfully
    ``user_language``. Here we re-read inside one session, mutate ONLY the
    JSON slice we own, and emit a targeted ``UPDATE notification_config``
    so other columns aren't touched at all.
    """
    async with AsyncSession(db.engine) as session:
        result = await session.execute(
            select(Users).where(Users.user_id == user_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            return
        cfg = user.get_notification_config()
        cfg["_attestation_state"] = {str(k): int(v) for k, v in state.items()}
        user.set_notification_config(cfg)
        await session.execute(
            update(Users)
            .where(Users.user_id == user_id)
            .values(notification_config=user.notification_config)
        )
        await session.commit()


async def update_operator_balance_state(user_id: int, state: dict) -> None:
    """Atomically refresh ``notification_config["_operator_balance_state"]``.

    Same pattern as :func:`update_attestation_state` — a targeted UPDATE on
    the JSON column so we don't ``merge()`` the rest of the row and clobber
    concurrent edits (language, tracking_data, etc.) made while the
    ~minute-long alert cycle was holding a stale Users snapshot.
    """
    async with AsyncSession(db.engine) as session:
        result = await session.execute(
            select(Users).where(Users.user_id == user_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            return
        cfg = user.get_notification_config()
        cfg["_operator_balance_state"] = {str(k): int(v) for k, v in state.items()}
        user.set_notification_config(cfg)
        await session.execute(
            update(Users)
            .where(Users.user_id == user_id)
            .values(notification_config=user.notification_config)
        )
        await session.commit()


async def clear_request_queue(user_id: int) -> None:
    """Atomically null out ``request_queue`` for a user.

    The queue worker reads a stale ``Users`` snapshot at the start of
    ``process_single_request`` and held it through the whole RPC fetch +
    Telegram render (~10 s). A blanket ``write_to_db(user)`` at the end
    via ``session.merge()`` rewrote every column from that snapshot,
    silently undoing any tracking_data / language / threshold edits the
    user made during the cycle (real bug: user deletes addresses while
    /get_full_info is in flight, addresses come back).

    A targeted ``UPDATE users SET request_queue = NULL WHERE user_id = ?``
    keeps the queue dequeue idempotent without touching anything else.
    """
    async with AsyncSession(db.engine) as session:
        await session.execute(
            update(Users)
            .where(Users.user_id == user_id)
            .values(request_queue=None)
        )
        await session.commit()


async def clear_notifications_if_empty(user_id: int) -> Optional[str]:
    """Wipe notification fields iff the user still has no tracked addresses.

    Used by the hourly notifier when its stale snapshot says the user
    deleted everything. Refetches before writing — if the user re-added
    something during the cycle, we leave their config alone.

    Returns the user's current language for the follow-up "no addresses"
    DM, or ``None`` if nothing was cleared.
    """
    async with AsyncSession(db.engine) as session:
        result = await session.execute(
            select(Users).where(Users.user_id == user_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None
        doc = user.get_tracking_data()
        if doc.get("validators") or doc.get("delegations"):
            return None
        await session.execute(
            update(Users)
            .where(Users.user_id == user_id)
            .values(claim_reward_msg=0, notification_config=None)
        )
        await session.commit()
        return user.user_language or "en"