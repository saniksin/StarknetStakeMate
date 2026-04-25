"""Idempotent DB migrations run on each bot boot.

Each ``migrate_*`` function:
  - detects whether the migration is needed (no destructive action otherwise);
  - does the change in one transaction;
  - logs ``migration completed`` so we can see it applied.

Call :func:`run_all` from ``main.py`` after ``initialize_db``.

Note: the old ``data_pair`` → ``{validators, delegations}`` migration has
been removed. On the switch to the staker-based delegation model the project
decided to wipe the DB instead of carrying migration code forward — there
was no safe way to infer the staker address from a stored pool address
without per-contract RPC lookups, and the user base was small enough to
re-add manually.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from db_api.database import db


async def _add_column_if_missing(session: AsyncSession, col: str, ddl: str) -> None:
    try:
        await session.execute(text(f"ALTER TABLE users ADD COLUMN {col} {ddl};"))
        await session.commit()
        logger.info(f"migration: added users.{col}")
    except OperationalError:
        await session.rollback()  # already exists — idempotent no-op


async def migrate_request_queue(session: AsyncSession) -> None:
    """Add ``request_queue`` column (previously in migrate_queue.py)."""
    await _add_column_if_missing(session, "request_queue", "TEXT DEFAULT NULL")


async def migrate_notification_config(session: AsyncSession) -> None:
    """Add ``notification_config`` column for Bug 4 thresholds.

    Stores a JSON document of the form
    ``{"usd_threshold": float, "token_thresholds": {symbol: float}}``.
    """
    await _add_column_if_missing(session, "notification_config", "TEXT DEFAULT NULL")


async def run_all() -> None:
    """Run every migration in declaration order; each is idempotent."""
    async with AsyncSession(db.engine) as session:
        await migrate_request_queue(session)
        await migrate_notification_config(session)
