"""xPay API client.

One module-level httpx.AsyncClient for the whole process, mirroring
bot/services/tg.py: constructing a client rebuilds the full certifi CA
bundle into a fresh SSL context, and doing that per request leaks memory
faster than the GC returns it to the OS.
"""

import asyncio
from datetime import datetime

import httpx

WEBHOOK_PATH = "/api/payment/xpay/webhook"

_client: httpx.AsyncClient | None = None
_token: str | None = None
_service_uuid: str | None = None
_expires_at: datetime | None = None
_token_lock = asyncio.Lock()
