from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

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
