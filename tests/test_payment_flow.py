import httpx
import pytest
import respx
from aiogram import Dispatcher
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.database.db import AsyncSessionLocal
from bot.database.models import Payment, User
from bot.handlers.ad_flow import AdFlow, confirm_payment
from bot.services import fsm

BASE = "https://devapi.xpay.kg"
LOGIN = f"{BASE}/api/v1/developer/login"


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


async def _seed_payment(qr_id="tx-1", status="WAITING", user_id=555):
    async with AsyncSessionLocal() as session:
        session.add(User(id=user_id, language="ru"))
        payment = Payment(
            user_id=user_id, qr_transaction_id=qr_id, amount=100.0, status=status
        )
        session.add(payment)
        await session.commit()
        return payment.id


def _mock_xpay(pay_status):
    """Mock login + one dynamic-status response."""
    from tests.test_xpay import login_body, status_body, status_url

    respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    return respx.get(status_url("tx-1")).mock(
        return_value=httpx.Response(200, json=status_body(pay_status))
    )


@respx.mock
async def test_confirm_returns_false_and_records_status_when_unpaid(db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "bot.handlers.ad_flow._notify_paid", lambda uid, lang: sent.append(uid)
    )
    payment_id = await _seed_payment()
    _mock_xpay("WAITING")

    assert await confirm_payment(payment_id) is False

    async with AsyncSessionLocal() as session:
        assert (await session.get(Payment, payment_id)).status == "WAITING"
    assert sent == [], "no confirmation message may be sent before payment lands"


@respx.mock
async def test_confirm_marks_paid_and_advances_state(db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "bot.handlers.ad_flow._notify_paid",
        lambda uid, lang: sent.append(uid),
    )
    dp = Dispatcher()
    fsm.set_dispatcher(dp)
    try:
        payment_id = await _seed_payment()
        _mock_xpay("COMPLETED")

        assert await confirm_payment(payment_id) is True

        async with AsyncSessionLocal() as session:
            assert (await session.get(Payment, payment_id)).status == "COMPLETED"
        assert await fsm.get_fsm_context(555).get_state() == AdFlow.content.state
        assert sent == [555]
    finally:
        fsm._dispatcher = None


@respx.mock
async def test_confirm_is_idempotent(db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "bot.handlers.ad_flow._notify_paid", lambda uid, lang: sent.append(uid)
    )
    payment_id = await _seed_payment(status="COMPLETED")
    route = _mock_xpay("COMPLETED")

    assert await confirm_payment(payment_id) is True

    assert route.call_count == 0, "an already-completed payment needs no API call"
    assert sent == [], "the confirmation message must not be sent twice"


@respx.mock
async def test_confirm_returns_false_when_xpay_is_unreachable(db, monkeypatch):
    monkeypatch.setattr("bot.handlers.ad_flow._notify_paid", lambda uid, lang: None)
    payment_id = await _seed_payment()
    respx.post(LOGIN).mock(side_effect=httpx.ConnectError("down"))

    assert await confirm_payment(payment_id) is False

    async with AsyncSessionLocal() as session:
        assert (await session.get(Payment, payment_id)).status == "WAITING"


async def test_confirm_returns_false_for_unknown_payment(db):
    assert await confirm_payment(999_999) is False
