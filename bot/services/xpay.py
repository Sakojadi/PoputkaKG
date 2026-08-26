"""xPay API client.

One module-level httpx.AsyncClient for the whole process, mirroring
bot/services/tg.py: constructing a client rebuilds the full certifi CA
bundle into a fresh SSL context, and doing that per request leaks memory
faster than the GC returns it to the OS.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from bot.config import config

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/api/payment/xpay/webhook"

_client: httpx.AsyncClient | None = None
_token: str | None = None
_service_uuid: str | None = None
_expires_at: datetime | None = None
_token_lock = asyncio.Lock()

REQUEST_TIMEOUT = 15.0
TOKEN_REFRESH_MARGIN = timedelta(seconds=60)
# Fallback lifetime when xPay sends an expires_at we cannot parse. The
# documented TTL is 30 minutes; 25 keeps us clear of the real expiry.
FALLBACK_TOKEN_TTL = timedelta(minutes=25)


class XPayError(Exception):
    """Any failure talking to xPay: transport, HTTP, or an Error status body."""


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.xpay_base_url,
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        )
    return _client


async def close_client() -> None:
    global _client, _token, _service_uuid, _expires_at
    if _client is not None:
        await _client.aclose()
    _client = None
    _token = None
    _service_uuid = None
    _expires_at = None


def _unwrap(path: str, resp: httpx.Response) -> dict:
    """Validate an xPay envelope and return its `data` object."""
    try:
        body = resp.json()
    except ValueError as e:
        raise XPayError(
            f"xPay {path} returned non-JSON (HTTP {resp.status_code})"
        ) from e
    if resp.status_code != 200 or body.get("status") != "Success":
        message = body.get("message") or "no message"
        raise XPayError(f"xPay {path} failed (HTTP {resp.status_code}): {message}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise XPayError(f"xPay {path} returned no data object")
    return data


async def _post_json(path: str, payload: dict, headers: dict | None = None) -> dict:
    try:
        resp = await get_client().post(path, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise XPayError(f"xPay request to {path} failed: {e}") from e
    return _unwrap(path, resp)


def _parse_expires(raw) -> datetime:
    """Parse xPay's `2026-02-09T10:52:55.000000Z` into an aware datetime."""
    now = datetime.now(UTC)
    if not raw:
        return now + FALLBACK_TOKEN_TTL
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        logger.warning(f"Unparseable xPay expires_at {raw!r}; assuming short TTL")
        return now + FALLBACK_TOKEN_TTL
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _login() -> tuple[str, str, datetime]:
    if not config.xpay_client_id or not config.xpay_client_secret:
        raise XPayError("XPAY_CLIENT_ID / XPAY_CLIENT_SECRET are not configured")

    data = await _post_json(
        "/api/v1/developer/login",
        {
            "client_id": config.xpay_client_id,
            "client_secret": config.xpay_client_secret,
        },
    )

    token = data.get("access_token")
    if not token:
        raise XPayError("xPay login returned no access_token")

    # The service uuid is never hardcoded: it is whichever merchant point
    # matches the mode we are configured for. A sandbox key used in
    # production mode therefore fails loudly instead of appearing to work.
    wanted = config.xpay_mode.upper()
    services = data.get("service") or []
    match = next(
        (s for s in services if str(s.get("mode", "")).upper() == wanted), None
    )
    if match is None:
        available = [s.get("mode") for s in services]
        raise XPayError(
            f"No xPay service with mode {wanted}; credentials expose {available}"
        )
    service_uuid = match.get("uuid")
    if not service_uuid:
        raise XPayError(f"xPay service with mode {wanted} has no uuid")

    return str(token), str(service_uuid), _parse_expires(data.get("expires_at"))


async def _get_auth() -> tuple[str, str]:
    """Return a live (access_token, service_uuid), logging in only if needed."""
    global _token, _service_uuid, _expires_at
    async with _token_lock:
        now = datetime.now(UTC)
        if (
            _token
            and _service_uuid
            and _expires_at
            and now < _expires_at - TOKEN_REFRESH_MARGIN
        ):
            return _token, _service_uuid
        _token, _service_uuid, _expires_at = await _login()
        logger.info(f"xPay token refreshed, mode={config.xpay_mode}")
        return _token, _service_uuid


# Shown to the payer inside their banking app.
SERVICE_NAME = "Poputka KG — реклама"


@dataclass(frozen=True)
class PaymentQR:
    qr_transaction_id: str
    qr_code: str
    qr_image: str


async def create_payment(user_id: int, amount_som: float) -> PaymentQR:
    """Create a dynamic QR for one order. Raises XPayError on any failure."""
    token, service_uuid = await _get_auth()

    payload = {
        "uuid": service_uuid,
        # The API takes tyiyn: 100 som == 10000. This is the only place in
        # the codebase that performs the conversion.
        "amount": round(amount_som * 100),
        "type": "dynamic",
        "payer_id": str(user_id),
        "service_name": SERVICE_NAME,
        "comments": f"{SERVICE_NAME} ({user_id})",
        "amount_change": False,
        "qr_pos": False,
    }

    # No public origin means local development: xPay cannot reach us, so we
    # omit callback_url and the flow relies on the user's check button.
    # check_url is deliberately never sent - it blocks the payment until our
    # server answers HTTP 201, which adds a failure mode and buys nothing.
    base_url = config.resolved_public_base_url
    if base_url:
        payload["callback_url"] = f"{base_url}{WEBHOOK_PATH}"

    data = await _post_json(
        "/api/v1/developer/qr/get",
        payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    qr_transaction_id = data.get("qr_transaction_id")
    if not qr_transaction_id:
        raise XPayError("xPay qr/get returned no qr_transaction_id")

    return PaymentQR(
        qr_transaction_id=str(qr_transaction_id),
        qr_code=str(data.get("qr_code") or ""),
        qr_image=str(data.get("qr_image") or ""),
    )


async def get_payment_status(qr_transaction_id: str) -> str:
    """Return the current pay_status, e.g. WAITING / COMPLETED / CANCELED.

    This is the only authority on whether money arrived. The webhook payload
    is unsigned and is never trusted for this.
    """
    token, _ = await _get_auth()
    path = f"/api/v1/developer/qr/dynamic/status/{qr_transaction_id}"
    try:
        resp = await get_client().get(
            path, headers={"Authorization": f"Bearer {token}"}
        )
    except httpx.HTTPError as e:
        raise XPayError(f"xPay request to {path} failed: {e}") from e

    data = _unwrap(path, resp)
    pay_status = data.get("pay_status")
    if not pay_status:
        raise XPayError(f"xPay {path} returned no pay_status")
    return str(pay_status).upper()
