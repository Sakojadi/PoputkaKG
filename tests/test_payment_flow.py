import pytest
from aiogram import Dispatcher
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.database.db import AsyncSessionLocal
from bot.database.models import Payment
from bot.handlers.ad_flow import AdFlow
from bot.services import fsm


async def test_payment_row_round_trips(db):
    async with AsyncSessionLocal() as session:
        session.add(
            Payment(
                user_id=555,
                qr_transaction_id="1770634567jZXWF3UsNotJyGu",
                amount=100.0,
                status="WAITING",
            )
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Payment).where(
                Payment.qr_transaction_id == "1770634567jZXWF3UsNotJyGu"
            )
        )
        payment = result.scalar_one()

    assert payment.user_id == 555
    assert payment.amount == 100.0
    assert payment.status == "WAITING"
    assert payment.campaign_id is None
    assert payment.created_at is not None


async def test_qr_transaction_id_uniqueness(db):
    """Verify qr_transaction_id is unique. This is load-bearing for webhook
    lookups in Task 7 — a duplicate would make the lookup ambiguous."""
    qr_id = "unique-txn-id-abc123"

    # Insert first payment
    async with AsyncSessionLocal() as session:
        session.add(
            Payment(
                user_id=111,
                qr_transaction_id=qr_id,
                amount=50.0,
                status="WAITING",
            )
        )
        await session.commit()

    # Attempt to insert second payment with same qr_transaction_id
    # Must be in its own session to avoid poisoning the first
    with pytest.raises(IntegrityError):
        async with AsyncSessionLocal() as session:
            session.add(
                Payment(
                    user_id=222,
                    qr_transaction_id=qr_id,
                    amount=75.0,
                    status="WAITING",
                )
            )
            await session.commit()


async def test_no_dispatcher_yields_no_context():
    fsm._dispatcher = None
    assert fsm.get_fsm_context(555) is None


async def test_registered_dispatcher_yields_a_usable_context():
    dp = Dispatcher()
    fsm.set_dispatcher(dp)
    try:
        ctx = fsm.get_fsm_context(555)
        assert ctx is not None
        await ctx.set_state(AdFlow.content)
        assert await ctx.get_state() == AdFlow.content.state
    finally:
        fsm._dispatcher = None


async def test_context_is_keyed_per_user():
    dp = Dispatcher()
    fsm.set_dispatcher(dp)
    try:
        await fsm.get_fsm_context(111).set_state(AdFlow.content)
        assert await fsm.get_fsm_context(222).get_state() is None
    finally:
        fsm._dispatcher = None
