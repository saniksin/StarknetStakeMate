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
    # Backfill the missing uniqueness contract on Users.user_id.
    #
    # The original schema declared user_id as a plain Integer column with
    # NO unique constraint. ``get_or_create_user`` then races on /start:
    # two parallel /start handlers both see ``get_account → None`` and
    # both ``session.merge`` a fresh row, so a single Telegram user ends
    # up with multiple ``users`` rows that share the same ``user_id``
    # but differ on the auto-PK ``id``. Once duplicates exist, every
    # ``scalar_one_or_none()`` lookup on that user_id raises
    # ``MultipleResultsFound`` and the whole notification loop crashes.
    #
    # We add the UNIQUE INDEX as a one-shot migration. ``IF NOT EXISTS``
    # keeps it safe to run on every boot. If duplicates are still in the
    # table, the CREATE will fail — we log and continue (the .first()
    # fallback in the helpers absorbs it for now); the operator is
    # expected to dedupe with the SQL in dedupe_users.sql, then a future
    # boot will succeed in installing the index.
    from sqlalchemy import text

    async with db.engine.begin() as conn:
        try:
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_user_id "
                "ON users(user_id)"
            ))
        except Exception as exc:  # noqa: BLE001
            from utils.logger import logger
            logger.warning(
                f"could not install UNIQUE INDEX on users.user_id "
                f"(duplicates likely exist — dedupe and restart): {exc}"
            )


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
        # Use ``.first()`` rather than ``scalar_one_or_none()``: the latter
        # raises ``MultipleResultsFound`` when stale duplicate rows exist
        # for the same ``user_id``. Such dupes shouldn't be there in the
        # first place (see UNIQUE INDEX in initialize_db) but if a legacy
        # DB still has them we'd rather quietly update the most recently
        # written row than crash the entire notification loop.
        user = result.scalars().first()
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


async def update_operator_balance_was_below(
    user_id: int, was_below: dict[str, bool]
) -> None:
    """Atomically refresh ``notification_config["_operator_balance_was_below"]``.

    Same pattern as :func:`update_attestation_state` — a targeted UPDATE on
    the JSON column so we don't ``merge()`` the rest of the row and clobber
    concurrent edits (language, tracking_data, etc.) made while the
    ~minute-long alert cycle was holding a stale Users snapshot.

    ``was_below`` keys are staker addresses, values booleans. Persistence
    keeps only True entries (absence means "above").
    """
    async with AsyncSession(db.engine) as session:
        result = await session.execute(
            select(Users).where(Users.user_id == user_id)
        )
        # Use ``.first()`` rather than ``scalar_one_or_none()``: the latter
        # raises ``MultipleResultsFound`` when stale duplicate rows exist
        # for the same ``user_id``. Such dupes shouldn't be there in the
        # first place (see UNIQUE INDEX in initialize_db) but if a legacy
        # DB still has them we'd rather quietly update the most recently
        # written row than crash the entire notification loop.
        user = result.scalars().first()
        if user is None:
            return
        cfg = user.get_notification_config()
        cfg["_operator_balance_was_below"] = {
            str(k): bool(v) for k, v in was_below.items() if v
        }
        user.set_notification_config(cfg)
        await session.execute(
            update(Users)
            .where(Users.user_id == user_id)
            .values(notification_config=user.notification_config)
        )
        await session.commit()


# Back-compat alias for legacy callers (none expected in-tree, but the
# webapp / migration scripts may still import the old name).
update_operator_balance_state = update_operator_balance_was_below


async def add_tracking_entry(
    user_id: int,
    *,
    kind: str,
    payload: dict,
) -> dict:
    """Atomically append a new entry to ``users.tracking_data``.

    Re-reads the row inside the session so two simultaneous tabs of the
    Mini App can't lose each other's writes the way ``snapshot + merge``
    would. ``payload`` is the entry dict (``{address, label}`` for
    validators or ``{delegator, staker, label}`` for delegations) —
    validation must already have happened upstream in
    :mod:`services.tracking_service`. Returns the resulting full doc so
    the caller can return it to the client.

    Raises :class:`ValueError` when the user row doesn't exist, mirroring
    the existing ``404 unknown user`` shape used by API endpoints.
    """
    from services.tracking_service import (
        AddTrackingError,
        MAX_TRACKED_ENTRIES,
        _normalize,
        dump_tracking,
        load_tracking,
    )

    if kind not in ("validator", "delegator"):
        raise ValueError(f"unknown kind: {kind!r}")

    list_key = "validators" if kind == "validator" else "delegations"

    async with AsyncSession(db.engine) as session:
        result = await session.execute(
            select(Users).where(Users.user_id == user_id)
        )
        user = result.scalars().first()
        if user is None:
            raise ValueError(f"user {user_id} not found")

        doc = _normalize(load_tracking(user.tracking_data))

        # Re-validate inside the transaction. Capacity + duplicate
        # checks live here too because the snapshot the service layer
        # validated against may be stale by the time we get the row
        # lock — another concurrent tab could have used the last slot.
        total = len(doc["validators"]) + len(doc["delegations"])
        if total >= MAX_TRACKED_ENTRIES:
            raise AddTrackingError(
                "limit_reached",
                f"max {MAX_TRACKED_ENTRIES} tracked entries per user",
            )
        if kind == "validator":
            new_addr = (payload.get("address") or "").lower()
            if any(
                (v.get("address") or "").lower() == new_addr
                for v in doc["validators"]
            ):
                raise AddTrackingError(
                    "duplicate", "validator already in your tracking list"
                )
        else:
            new_del = (payload.get("delegator") or "").lower()
            new_sta = (payload.get("staker") or "").lower()
            if any(
                (d.get("delegator") or "").lower() == new_del
                and (d.get("staker") or "").lower() == new_sta
                for d in doc["delegations"]
            ):
                raise AddTrackingError(
                    "duplicate", "delegation already in your tracking list"
                )

        doc[list_key].append(payload)
        new_json = dump_tracking(doc)

        await session.execute(
            update(Users)
            .where(Users.user_id == user_id)
            .values(tracking_data=new_json)
        )
        await session.commit()
        return doc


async def reorder_tracking_entries(
    user_id: int,
    *,
    order: list[str] | None = None,
    validators_order: list[str] | None = None,
    delegations_order: list[tuple[str, str]] | None = None,
) -> dict:
    """Atomically update the user's display_order.

    Same atomicity story as :func:`add_tracking_entry` — re-read inside
    one session, mutate the JSON snapshot, emit a single targeted
    ``UPDATE``. Keeps a concurrent tab's add (which writes the same
    column) from losing this reorder, and vice-versa.

    Two parameter shapes are accepted; the API layer enforces mutual
    exclusivity (passing both is a 422), so by the time we get here at
    most one is non-None:

      - ``order`` (preferred): flat list of stable identity keys, used
        for cross-group reorder. Routed to ``reorder_tracking_doc_v2``.
      - ``validators_order`` / ``delegations_order`` (legacy): two-list
        shape from cached PWA clients; routed to the back-compat shim
        ``reorder_tracking_doc`` which synthesises a flat order and
        delegates to v2 internally.

    Returns the resulting full doc. Raises :class:`ValueError` when the
    user row doesn't exist.
    """
    from services.tracking_service import (
        dump_tracking,
        load_tracking,
        reorder_tracking_doc,
        reorder_tracking_doc_v2,
    )

    async with AsyncSession(db.engine) as session:
        result = await session.execute(
            select(Users).where(Users.user_id == user_id)
        )
        user = result.scalars().first()
        if user is None:
            raise ValueError(f"user {user_id} not found")

        doc = load_tracking(user.tracking_data)
        if order is not None:
            new_doc = reorder_tracking_doc_v2(doc, order=order)
        else:
            # Either both are None (no-op — write through dump_tracking
            # which still preserves display_order if any) or one of
            # validators_order/delegations_order is set; the shim handles
            # both paths.
            new_doc = reorder_tracking_doc(
                doc,
                validators_order=validators_order,
                delegations_order=delegations_order,
            )
        new_json = dump_tracking(new_doc)

        await session.execute(
            update(Users)
            .where(Users.user_id == user_id)
            .values(tracking_data=new_json)
        )
        await session.commit()
        return new_doc


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
        # Use ``.first()`` rather than ``scalar_one_or_none()``: the latter
        # raises ``MultipleResultsFound`` when stale duplicate rows exist
        # for the same ``user_id``. Such dupes shouldn't be there in the
        # first place (see UNIQUE INDEX in initialize_db) but if a legacy
        # DB still has them we'd rather quietly update the most recently
        # written row than crash the entire notification loop.
        user = result.scalars().first()
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