from sqlalchemy.ext.asyncio import AsyncSession
from db_api.models import Users
from db_api.database import db, get_account, write_to_db


async def get_or_create_user(user_id, user_name, user_language, registration_date):
        print('new user: ', end='')
        print(user_id, user_name, user_language, registration_date)
        user = await get_account(user_id)
        if not user:
            async with AsyncSession(db.engine) as session:
                user = Users(
                    user_id=user_id,
                    user_name=user_name,
                    user_language=user_language,
                    registration_data=registration_date,
                )
                await session.merge(user)
                await session.commit()
        else:
            if user_name != user.user_name:
                user.user_name = user_name
                await write_to_db(user)
        return user
