import uuid


async def generate_xpay_link(amount: float) -> tuple[str, str]:
    """Mock implementation: returns a fake xpay link and a unique payment id."""
    payment_id = str(uuid.uuid4())
    link = f"https://pay.xpay.kg/mock/{payment_id}?amount={amount}"
    return link, payment_id


async def check_xpay_payment(payment_id: str) -> bool:
    """Mock implementation: simulates checking payment. For vibe coding, we'll pretend it's paid."""
    # To make it realistic, you can toggle this or check a mock DB.
    # We will just return True to simulate immediate successful payment.
    return True
