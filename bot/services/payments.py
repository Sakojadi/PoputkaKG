"""Payment provider selection.

Two providers sit behind one interface:

- ``mock``  — returns a placeholder link and reports every payment as paid.
  No network call, no money, no verification of any kind.
- ``xpay``  — the real xPay integration in :mod:`bot.services.xpay`.

Which one runs is decided by ``PAYMENT_PROVIDER`` at call time, so switching
is an environment change rather than a code change. The provider is read on
every call rather than captured at import, so the answer always follows the
live settings object.
"""

import logging
import uuid

import bot.config
from bot.services import xpay
from bot.services.xpay import PaymentQR, XPayError

logger = logging.getLogger(__name__)

# The bot layer catches this name; xPay is simply the only provider that can
# actually fail, so its error type is the shared one.
PaymentError = XPayError

MOCK_LINK_BASE = "https://pay.xpay.kg/mock"

__all__ = [
    "PaymentError",
    "PaymentQR",
    "create_payment",
    "get_payment_status",
    "is_mock",
]


def is_mock() -> bool:
    # Read through the module rather than binding `config` at import time, so
    # the answer follows the live settings object.
    return bot.config.config.payment_provider == "mock"


async def _mock_create_payment(user_id: int, amount_som: float) -> PaymentQR:
    transaction_id = f"mock-{uuid.uuid4()}"
    logger.info(
        f"Mock payment {transaction_id} for user {user_id}, {amount_som} som "
        "(no charge, will report COMPLETED)"
    )
    return PaymentQR(
        qr_transaction_id=transaction_id,
        qr_code=f"{MOCK_LINK_BASE}/{transaction_id}?amount={amount_som}",
        # Empty on purpose: there is no real QR to render, and the handler
        # falls back to a plain text message when this is blank.
        qr_image="",
    )


async def create_payment(user_id: int, amount_som: float) -> PaymentQR:
    """Start one payment. Raises PaymentError on any provider failure."""
    if is_mock():
        return await _mock_create_payment(user_id, amount_som)
    return await xpay.create_payment(user_id, amount_som)


async def get_payment_status(qr_transaction_id: str) -> str:
    """Return the provider's status string, e.g. WAITING / COMPLETED."""
    if is_mock():
        # The mock provider does not check anything. Every payment is paid.
        return "COMPLETED"
    return await xpay.get_payment_status(qr_transaction_id)
