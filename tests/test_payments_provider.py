"""The mock/xpay provider switch."""

import pytest
import respx

import bot.config
from bot.config import PAYMENT_PROVIDERS, Settings
from bot.database.db import AsyncSessionLocal
from bot.database.models import Payment
from bot.services import payments
from bot.web.app import xpay_webhook
from tests.test_webhook import FakeRequest


def test_mock_is_the_shipped_default():
    """Changing this default silently starts charging real money."""
    assert Settings(bot_token="x").payment_provider == "mock"


def test_provider_names_are_validated():
    assert set(PAYMENT_PROVIDERS) == {"mock", "xpay"}
    with pytest.raises(ValueError, match="PAYMENT_PROVIDER"):
        Settings(bot_token="x", payment_provider="sandbox")


@pytest.mark.parametrize("raw", ["MOCK", " 'xpay' ", "XPay"])
def test_provider_is_normalised(raw):
    assert Settings(bot_token="x", payment_provider=raw).payment_provider in (
        "mock",
        "xpay",
    )


@respx.mock
async def test_mock_provider_makes_no_network_call(use_mock_payments):
    """respx with no routes registered raises on any outbound request."""
    qr = await payments.create_payment(user_id=42, amount_som=250.0)

    assert qr.qr_transaction_id.startswith("mock-")
    assert qr.qr_code.startswith(payments.MOCK_LINK_BASE)
    assert "250.0" in qr.qr_code
    # No real QR exists, so the handler must fall back to a text message.
    assert qr.qr_image == ""


async def test_mock_provider_always_reports_paid(use_mock_payments):
    assert await payments.get_payment_status("anything-at-all") == "COMPLETED"


async def test_mock_transaction_ids_are_unique(use_mock_payments):
    first = await payments.create_payment(1, 10.0)
    second = await payments.create_payment(1, 10.0)
    assert first.qr_transaction_id != second.qr_transaction_id


async def test_is_mock_follows_config(use_xpay):
    assert payments.is_mock() is False
    bot.config.config.payment_provider = "mock"
    assert payments.is_mock() is True


async def test_webhook_is_inert_under_the_mock_provider(db, use_mock_payments):
    """Otherwise a guessed transaction id would settle a payment for free."""
    async with AsyncSessionLocal() as session:
        payment = Payment(
            user_id=1,
            qr_transaction_id="mock-guessable",
            status="WAITING",
        )
        session.add(payment)
        await session.commit()
        payment_id = payment.id

    result = await xpay_webhook(
        FakeRequest({"qr_transaction_id": "mock-guessable", "status": "COMPLETED"})
    )

    assert result == {"status": "ok"}
    async with AsyncSessionLocal() as session:
        assert (await session.get(Payment, payment_id)).status == "WAITING"
