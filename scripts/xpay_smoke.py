"""Manual probe against the real xPay sandbox. Not part of the test suite.

Run:  uv run python scripts/xpay_smoke.py
Then: open https://sandbox.xpay.kg, pay the printed transaction, and run
      uv run python scripts/xpay_smoke.py <qr_transaction_id>
"""

import asyncio
import sys

from bot.config import config
from bot.services.xpay import (
    _get_auth,
    close_client,
    create_payment,
    get_payment_status,
)


async def main() -> None:
    print(f"mode={config.xpay_mode} base_url={config.xpay_base_url}")
    print(f"callback_url base={config.resolved_public_base_url or '(none, local)'}")

    if len(sys.argv) > 1:
        qr_transaction_id = sys.argv[1]
        print(f"status={await get_payment_status(qr_transaction_id)}")
        return

    token, service_uuid = await _get_auth()
    print(f"token={token[:12]}... service_uuid={service_uuid}")

    qr = await create_payment(user_id=999999, amount_som=1.0)
    print(f"qr_transaction_id={qr.qr_transaction_id}")
    print(f"qr_code={qr.qr_code}")
    print(f"qr_image={qr.qr_image}")
    print(f"status={await get_payment_status(qr.qr_transaction_id)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        asyncio.run(close_client())
