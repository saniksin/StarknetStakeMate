import json
from data.models import AutoRepr
from sqlalchemy import (Column, Integer, Text, Boolean, DateTime)
from sqlalchemy.orm import declarative_base


Base = declarative_base()

class Users(Base, AutoRepr):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer)
    user_name = Column(Text)
    user_language = Column(Text)
    user_registration_data = Column(DateTime)
    user_is_blocked = Column(Boolean, default=False)
    tracking_data = Column(Text, nullable=True)
    claim_reward_msg = Column(Integer)
   
    def __init__(
            self,
            user_id: int,
            user_name: str,
            user_language: str,
            registration_data: str,
    ) -> None:
        self.user_id = user_id
        self.user_name = user_name
        self.user_language = user_language 
        self.user_registration_data = registration_data
        self.tracking_data = json.dumps({"data_pair": []})
        self.claim_reward_msg = 0
        
    # Метод для получения отслеживаемых данных в виде словаря
    def get_tracking_data(self) -> dict:
        """
        Возвращает tracking_data как словарь.
        Если tracking_data пусто, возвращает словарь с пустыми списками wallet_addresses и pools.
        """
        if self.tracking_data:
            try:
                return json.loads(self.tracking_data)
            except json.JSONDecodeError:
                return {"data_pair": []}
        else:
            return {"data_pair": []}