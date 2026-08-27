import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from bot.config import config
from bot.services import xpay

BASE = "https://devapi.xpay.kg"
LOGIN = f"{BASE}/api/v1/developer/login"
SERVICE_UUID = "485b0981-8e71-41bf-97fd-4e8769e8cd9f"


def login_body(mode="SANDBOX", minutes=30):
    expires = datetime.now(UTC) + timedelta(minutes=minutes)
    return {
        "status": "Success",
        "message": "Developer LogIn",
        "data": {
            "access_token": "210957|TESTTOKEN",
            "token_type": "Bearer",
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
            "service": [{"uuid": SERVICE_UUID, "mode": mode, "tsp_serial": "ECOMTEST"}],
        },
    }


@respx.mock
async def test_login_returns_token_and_service_uuid():
    route = respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    token, service_uuid = await xpay._get_auth()
    assert token == "210957|TESTTOKEN"
    assert service_uuid == SERVICE_UUID
    assert route.call_count == 1


@respx.mock
async def test_token_is_cached_across_calls():
    route = respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    await xpay._get_auth()
    await xpay._get_auth()
    assert route.call_count == 1, "second call must reuse the cached token"


@respx.mock
async def test_token_is_refreshed_inside_the_safety_margin():
    # Expires in 30 seconds, i.e. inside the 60-second refresh margin.
    route = respx.post(LOGIN).mock(
        return_value=httpx.Response(200, json=login_body(minutes=0.5))
    )
    await xpay._get_auth()
    await xpay._get_auth()
    assert route.call_count == 2, "a near-expired token must be re-fetched"


@respx.mock
async def test_service_whose_mode_does_not_match_is_rejected():
    respx.post(LOGIN).mock(
        return_value=httpx.Response(200, json=login_body(mode="PRODUCTION"))
    )
    with pytest.raises(xpay.XPayError, match="SANDBOX"):
        await xpay._get_auth()


@respx.mock
async def test_error_status_body_raises():
    respx.post(LOGIN).mock(
        return_value=httpx.Response(
            200, json={"status": "Error", "message": "Invalid credentials", "data": None}
        )
    )
    with pytest.raises(xpay.XPayError, match="Invalid credentials"):
        await xpay._get_auth()


@respx.mock
async def test_transport_failure_raises_xpay_error():
    respx.post(LOGIN).mock(side_effect=httpx.ConnectError("no route to host"))
    with pytest.raises(xpay.XPayError):
        await xpay._get_auth()


@respx.mock
async def test_concurrent_callers_only_log_in_once():
    import asyncio

    route = respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    await asyncio.gather(*(xpay._get_auth() for _ in range(5)))
    assert route.call_count == 1, "the token lock must prevent a login stampede"


QR_GET = f"{BASE}/api/v1/developer/qr/get"


def qr_body():
    return {
        "status": "Success",
        "message": "Generate QR Code",
        "data": {
            "qr_transaction_id": "1770634567jZXWF3UsNotJyGu",
            "qr_code": "https://devpay.xpay.kg#0002010102123261...",
            "qr_image": "https://devimage.xpay.kg/000201010212...png",
        },
    }


def status_url(qr_id):
    return f"{BASE}/api/v1/developer/qr/dynamic/status/{qr_id}"


def status_body(pay_status):
    return {
        "status": "Success",
        "message": "Status QR Code",
        "data": {"qr_transaction_id": "abc", "pay_status": pay_status},
    }


@respx.mock
async def test_amount_is_converted_to_tyiyn():
    respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    route = respx.post(QR_GET).mock(return_value=httpx.Response(200, json=qr_body()))

    await xpay.create_payment(user_id=555, amount_som=100.0)

    sent = json.loads(route.calls.last.request.content)
    assert sent["amount"] == 10000, "100 som must be sent as 10000 tyiyn"


@respx.mock
async def test_fractional_amount_rounds_rather_than_truncates():
    respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    route = respx.post(QR_GET).mock(return_value=httpx.Response(200, json=qr_body()))

    await xpay.create_payment(user_id=555, amount_som=10.29)

    sent = json.loads(route.calls.last.request.content)
    assert sent["amount"] == 1029


@respx.mock
async def test_qr_request_carries_service_uuid_and_payer_id():
    respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    route = respx.post(QR_GET).mock(return_value=httpx.Response(200, json=qr_body()))

    result = await xpay.create_payment(user_id=555, amount_som=100.0)

    sent = json.loads(route.calls.last.request.content)
    assert sent["uuid"] == SERVICE_UUID
    assert sent["payer_id"] == "555"
    assert sent["type"] == "dynamic"
    assert sent["amount_change"] is False
    assert result.qr_transaction_id == "1770634567jZXWF3UsNotJyGu"
    assert result.qr_image.endswith(".png")


@respx.mock
async def test_check_url_is_never_sent():
    respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    route = respx.post(QR_GET).mock(return_value=httpx.Response(200, json=qr_body()))

    await xpay.create_payment(user_id=555, amount_som=100.0)

    sent = json.loads(route.calls.last.request.content)
    assert "check_url" not in sent, "check_url would block payment on our uptime"


@respx.mock
async def test_callback_url_is_omitted_when_not_publicly_reachable(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.setattr(config, "public_base_url", "")

    respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    route = respx.post(QR_GET).mock(return_value=httpx.Response(200, json=qr_body()))

    await xpay.create_payment(user_id=555, amount_som=100.0)

    sent = json.loads(route.calls.last.request.content)
    assert "callback_url" not in sent


@respx.mock
async def test_callback_url_is_sent_when_publicly_reachable(monkeypatch):
    monkeypatch.setattr(config, "public_base_url", "https://poputka.up.railway.app")

    respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    route = respx.post(QR_GET).mock(return_value=httpx.Response(200, json=qr_body()))

    await xpay.create_payment(user_id=555, amount_som=100.0)

    sent = json.loads(route.calls.last.request.content)
    assert sent["callback_url"] == (
        "https://poputka.up.railway.app/api/payment/xpay/webhook"
    )


@respx.mock
async def test_status_returns_uppercased_pay_status():
    respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    respx.get(status_url("abc")).mock(
        return_value=httpx.Response(200, json=status_body("completed"))
    )
    assert await xpay.get_payment_status("abc") == "COMPLETED"


@respx.mock
async def test_status_sends_bearer_token():
    respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    route = respx.get(status_url("abc")).mock(
        return_value=httpx.Response(200, json=status_body("WAITING"))
    )
    await xpay.get_payment_status("abc")
    assert route.calls.last.request.headers["Authorization"] == "Bearer 210957|TESTTOKEN"


@respx.mock
async def test_status_transport_failure_raises():
    respx.post(LOGIN).mock(return_value=httpx.Response(200, json=login_body()))
    respx.get(status_url("abc")).mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(xpay.XPayError):
        await xpay.get_payment_status("abc")
