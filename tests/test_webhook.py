import httpx
import respx

from bot.database.db import AsyncSessionLocal
from bot.database.models import Payment
from bot.services.xpay import WEBHOOK_PATH
from bot.web.app import app, xpay_webhook


class FakeRequest:
    """Minimal stand-in for starlette's Request: the handler only calls .json()."""

    def __init__(self, payload=None, raise_on_json=False):
        self._payload = payload
        self._raise = raise_on_json

    async def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._payload


async def _seed(qr_id="tx-1", status="WAITING"):
    async with AsyncSessionLocal() as session:
        payment = Payment(
            user_id=555, qr_transaction_id=qr_id, amount=100.0, status=status
        )
        session.add(payment)
        await session.commit()
        return payment.id


def _mock_status(pay_status):
    from tests.test_xpay import BASE, login_body, status_body, status_url

    respx.post(f"{BASE}/api/v1/developer/login").mock(
        return_value=httpx.Response(200, json=login_body())
    )
    return respx.get(status_url("tx-1")).mock(
        return_value=httpx.Response(200, json=status_body(pay_status))
    )


@respx.mock
async def test_webhook_confirms_a_paid_transaction(db, monkeypatch):
    monkeypatch.setattr("bot.handlers.ad_flow._notify_paid", lambda uid, lang: None)
    payment_id = await _seed()
    _mock_status("COMPLETED")

    result = await xpay_webhook(FakeRequest({"qr_transaction_id": "tx-1"}))

    assert result == {"status": "ok"}
    async with AsyncSessionLocal() as session:
        assert (await session.get(Payment, payment_id)).status == "COMPLETED"


@respx.mock
async def test_webhook_never_trusts_the_body(db, monkeypatch):
    """A body claiming COMPLETED must not confirm an unpaid transaction."""
    monkeypatch.setattr("bot.handlers.ad_flow._notify_paid", lambda uid, lang: None)
    payment_id = await _seed()
    _mock_status("WAITING")

    result = await xpay_webhook(
        FakeRequest({"qr_transaction_id": "tx-1", "pay_status": "COMPLETED"})
    )

    assert result == {"status": "ok"}
    async with AsyncSessionLocal() as session:
        assert (await session.get(Payment, payment_id)).status == "WAITING"


@respx.mock
async def test_webhook_reads_a_nested_data_payload(db, monkeypatch):
    monkeypatch.setattr("bot.handlers.ad_flow._notify_paid", lambda uid, lang: None)
    payment_id = await _seed()
    _mock_status("COMPLETED")

    result = await xpay_webhook(FakeRequest({"data": {"qr_transaction_id": "tx-1"}}))

    assert result == {"status": "ok"}
    async with AsyncSessionLocal() as session:
        assert (await session.get(Payment, payment_id)).status == "COMPLETED"


@respx.mock
async def test_unknown_transaction_is_ignored_without_an_api_call(db):
    route = _mock_status("COMPLETED")
    result = await xpay_webhook(FakeRequest({"qr_transaction_id": "not-ours"}))
    assert result == {"status": "ok"}
    assert route.call_count == 0


async def test_malformed_body_is_swallowed():
    assert await xpay_webhook(FakeRequest(raise_on_json=True)) == {"status": "ok"}


async def test_missing_transaction_id_is_swallowed():
    assert await xpay_webhook(FakeRequest({"hello": "world"})) == {"status": "ok"}


async def test_non_dict_body_is_swallowed():
    assert await xpay_webhook(FakeRequest(["not", "a", "dict"])) == {"status": "ok"}


def test_webhook_route_is_registered_and_public():
    """xPay cannot present our admin cookie, so the route must carry no auth."""
    route = next(
        r for r in app.routes if getattr(r, "path", None) == WEBHOOK_PATH
    )
    assert "POST" in route.methods
    assert route.dependant.dependencies == [], "the webhook must not require_admin"
