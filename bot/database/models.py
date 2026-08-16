from sqlalchemy import Column, Integer, String, Boolean, BigInteger
from .db import Base

class User(Base):
    __tablename__ = 'users'

    id = Column(BigInteger, primary_key=True)
    language = Column(String, default="ky")

class Campaign(Base):
    __tablename__ = 'campaigns'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    publications_total = Column(Integer)
    publications_left = Column(Integer)
    interval_minutes = Column(Integer)
    content_text = Column(String, nullable=True)
    content_photo = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    job_id = Column(String, nullable=True)
