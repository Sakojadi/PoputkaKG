import httpx
import pytest
import respx
from aiogram import Dispatcher
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.database.db import AsyncSessionLocal
from bot.database.models import Campaign, Payment, User
from bot.handlers.ad_flow import (
    AdFlow,
    confirm_payment,
    process_content,
    process_content_ok,
    process_payment,
)
from bot.services import fsm

BASE = "https://devapi.xpay.kg"
LOGIN = f"{BASE}/api/v1/developer/login"

# These tests drive the real xPay client with its HTTP calls mocked, so they
# have to opt out of the shipped default (the mock provider).
pytestmark = pytest.mark.usefixtures("use_xpay")


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
        ctx = fsm.get_fsm_context(555)
        await ctx.set_state(AdFlow.waiting_payment)
        await ctx.update_data(payment_db_id=payment_id)
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


@respx.mock
async def test_confirm_advances_a_stranded_already_completed_user(db, monkeypatch):
    """A webhook may have marked the row COMPLETED already (e.g. because the
    dispatcher was not registered yet, or the notify send failed) while the
    user's FSM is still stuck in waiting_payment. The check button must still
    be able to rescue them: no second API call, but the state must advance
    and the confirmation must be sent."""
    sent = []
    monkeypatch.setattr(
        "bot.handlers.ad_flow._notify_paid", lambda uid, lang: sent.append(uid)
    )
    dp = Dispatcher()
    fsm.set_dispatcher(dp)
    try:
        payment_id = await _seed_payment(status="COMPLETED")
        ctx = fsm.get_fsm_context(555)
        await ctx.set_state(AdFlow.waiting_payment)
        await ctx.update_data(payment_db_id=payment_id)
        route = _mock_xpay("COMPLETED")

        assert await confirm_payment(payment_id) is True

        assert route.call_count == 0, "an already-completed payment needs no API call"
        assert await fsm.get_fsm_context(555).get_state() == AdFlow.content.state
        assert sent == [555]
    finally:
        fsm._dispatcher = None


@respx.mock
async def test_confirm_does_not_rewind_a_user_past_waiting_payment(db, monkeypatch):
    """A duplicate webhook delivery for an already-completed payment must not
    knock a user who has already moved on (e.g. into confirm_content) back
    to content, nor re-notify them."""
    sent = []
    monkeypatch.setattr(
        "bot.handlers.ad_flow._notify_paid", lambda uid, lang: sent.append(uid)
    )
    dp = Dispatcher()
    fsm.set_dispatcher(dp)
    try:
        payment_id = await _seed_payment(status="COMPLETED")
        await fsm.get_fsm_context(555).set_state(AdFlow.confirm_content)
        route = _mock_xpay("COMPLETED")

        assert await confirm_payment(payment_id) is True

        assert route.call_count == 0, "an already-completed payment needs no API call"
        assert (
            await fsm.get_fsm_context(555).get_state() == AdFlow.confirm_content.state
        )
        assert sent == [], "a user already past waiting_payment must not be re-notified"
    finally:
        fsm._dispatcher = None


# --- Fakes for driving the aiogram handlers directly (no real Telegram calls) ---


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeMessage:
    """Minimal stand-in for aiogram's Message: only the attributes/methods the
    handlers under test actually touch."""

    def __init__(self, text=None, photo=None):
        self.text = text
        self.caption = None
        self.photo = photo

    async def delete(self):
        pass

    async def answer(self, *args, **kwargs):
        return FakeMessage()

    async def answer_photo(self, *args, **kwargs):
        return FakeMessage()

    async def edit_text(self, *args, **kwargs):
        pass


class FakeCallback:
    """Minimal stand-in for aiogram's CallbackQuery."""

    def __init__(self, user_id, data=None):
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.data = data

    async def answer(self, *args, **kwargs):
        pass


@respx.mock
async def test_full_payment_flow_links_amount_and_campaign(db, monkeypatch):
    """Drive process_payment -> confirm_payment (as the webhook would) ->
    process_content -> process_content_ok, and pin the money seam: the
    Payment amount must equal count * price_per_ad, must equal what the
    Campaign records as price_paid, and the two rows must be linked."""
    from tests.test_xpay import login_body, qr_body, status_body, status_url

    monkeypatch.setattr("bot.handlers.ad_flow._notify_paid", lambda uid, lang: None)

    user_id = 777
    async with AsyncSessionLocal() as session:
        session.add(User(id=user_id, language="ru"))
        await session.commit()

    respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    respx.post(f"{BASE}/api/v1/developer/qr/get").mock(
        return_value=httpx.Response(200, json=qr_body())
    )
    respx.get(status_url(qr_body()["data"]["qr_transaction_id"])).mock(
        return_value=httpx.Response(200, json=status_body("COMPLETED"))
    )

    dp = Dispatcher()
    fsm.set_dispatcher(dp)
    try:
        state = fsm.get_fsm_context(user_id)
        await state.set_state(AdFlow.confirm_payment)
        # price mirrors what process_interval locks in on the summary screen:
        # count(3) * DEFAULT_PRICE_PER_AD(1.0).
        await state.update_data(lang="ru", count=3, interval=5, price=3.0)

        callback = FakeCallback(user_id)
        await process_payment(callback, state)

        data = await state.get_data()
        payment_id = data["payment_db_id"]
        price = data["price"]
        assert price == 3.0  # count(3) * DEFAULT_PRICE_PER_AD(1.0)
        assert await state.get_state() == AdFlow.waiting_payment.state

        # Simulate the webhook settling the payment out of band.
        assert await confirm_payment(payment_id) is True
        assert await state.get_state() == AdFlow.content.state

        message = FakeMessage(text="Свежий чай на вынос")
        await process_content(message, state, bot=None)
        assert await state.get_state() == AdFlow.confirm_content.state

        # Snapshot the pre-commit data, mirroring what a second concurrent
        # "content_ok" tap (both reading state before either commits) would
        # see -- state.clear() below wipes it for the first tap.
        data_snapshot = await state.get_data()

        await process_content_ok(callback, state)

        async with AsyncSessionLocal() as session:
            payment = await session.get(Payment, payment_id)
            camp_res = await session.execute(
                select(Campaign).where(Campaign.user_id == user_id)
            )
            campaign = camp_res.scalar_one()

        assert payment.amount == price
        assert payment.amount == campaign.price_paid
        assert payment.campaign_id == campaign.id

        # A double-tap on "content_ok" (laggy connection) must not mint a
        # second campaign for the same payment.
        await state.set_data(data_snapshot)
        await process_content_ok(callback, state)
        async with AsyncSessionLocal() as session:
            camp_count = (
                await session.execute(
                    select(func.count(Campaign.id)).where(
                        Campaign.user_id == user_id
                    )
                )
            ).scalar()
        assert camp_count == 1
    finally:
        fsm._dispatcher = None


@respx.mock
async def test_paying_a_stale_qr_does_not_settle_the_newer_order(db, monkeypatch):
    """Regression test for Fix 1: a user abandons payment #1, restarts, and is
    now waiting on payment #2. Paying (or a webhook settling) the abandoned
    payment #1 must NOT advance the user, since they are not waiting on it.

    This must fail (advance the user) without the payment-specific guard in
    _advance_to_content, and pass with it -- see the fix report for the
    RED/GREEN run that proves this."""
    sent = []
    monkeypatch.setattr(
        "bot.handlers.ad_flow._notify_paid", lambda uid, lang: sent.append(uid)
    )
    dp = Dispatcher()
    fsm.set_dispatcher(dp)
    try:
        user_id = 555
        async with AsyncSessionLocal() as session:
            session.add(User(id=user_id, language="ru"))
            payment_1 = Payment(
                user_id=user_id, qr_transaction_id="tx-old", amount=100.0,
                status="WAITING",
            )
            payment_2 = Payment(
                user_id=user_id, qr_transaction_id="tx-1", amount=1000.0,
                status="WAITING",
            )
            session.add_all([payment_1, payment_2])
            await session.commit()
            payment_1_id = payment_1.id
            payment_2_id = payment_2.id

        state = fsm.get_fsm_context(user_id)
        await state.set_state(AdFlow.waiting_payment)
        await state.update_data(payment_db_id=payment_2_id, price=1000.0, count=10)

        from tests.test_xpay import login_body, status_body

        respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
        respx.get(f"{BASE}/api/v1/developer/qr/dynamic/status/tx-old").mock(
            return_value=httpx.Response(200, json=status_body("COMPLETED"))
        )

        # The stale, abandoned payment #1 gets paid and settled.
        assert await confirm_payment(payment_1_id) is True

        async with AsyncSessionLocal() as session:
            assert (await session.get(Payment, payment_1_id)).status == "COMPLETED"

        # The user must NOT be advanced: they are waiting on payment #2, not #1.
        assert await state.get_state() == AdFlow.waiting_payment.state
        assert (await state.get_data())["payment_db_id"] == payment_2_id
        assert sent == [], "the user must not be notified for a payment they are not waiting on"
    finally:
        fsm._dispatcher = None
