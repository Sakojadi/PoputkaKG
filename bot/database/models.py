from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)

from .db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    language = Column(String, default="ky")
    is_banned = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    publications_total = Column(Integer)
    publications_left = Column(Integer)
    interval_minutes = Column(Integer)
    content_text = Column(String, nullable=True)
    content_photo = Column(String, nullable=True)
    price_paid = Column(Float, default=0.0)
    is_active = Column(Boolean, default=True)
    job_id = Column(String, nullable=True)
    failure_count = Column(Integer, default=0)
    last_message_id = Column(BigInteger, nullable=True)
    message_ids = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(String)


class GroupPost(Base):
    __tablename__ = "group_posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(Text)
    buttons = Column(Text, default="[]")
    repeats_total = Column(Integer)
    repeats_left = Column(Integer)
    interval_minutes = Column(Integer)
    is_active = Column(Boolean, default=True)
    job_id = Column(String, nullable=True)
    failure_count = Column(Integer, default=0)
    last_message_id = Column(BigInteger, nullable=True)
    message_ids = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class Payment(Base):
    """One xPay payment attempt.

    Required rather than optional: the webhook arrives out of band with no
    FSM context, so qr_transaction_id must be resolvable to a user from
    durable storage.
    """

    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    qr_transaction_id = Column(String, unique=True, index=True)
    amount = Column(Float, default=0.0)  # som, what we charged
    status = Column(String, default="WAITING")
    campaign_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
