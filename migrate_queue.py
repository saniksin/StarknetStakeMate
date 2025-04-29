import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError

from db_api.database import db
from data.languages import logger


async def migrate():
    async with AsyncSession(db.engine) as session:
        try:
            await session.execute(
                text("""
                    ALTER TABLE users
                    ADD COLUMN request_queue TEXT DEFAULT NULL;
                """)
            )
            await session.commit()
            logger.success('Queue migration completed.')
        except OperationalError as e:
            logger.error(f'Error during queue migration: {e}')
        finally:
            await session.close() 