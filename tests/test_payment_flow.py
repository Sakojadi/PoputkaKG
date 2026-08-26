import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.database.db import AsyncSessionLocal
from bot.database.models import Payment


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
