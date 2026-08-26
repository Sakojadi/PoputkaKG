# xPay Sandbox Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mock payment stub with a real xPay QR integration running against the sandbox, wired so that going live is an environment-variable change.

**Architecture:** A standalone async API client (`bot/services/xpay.py`) owns all HTTP contact with xPay and knows nothing about Telegram. A `Payment` table makes a transaction id resolvable to a user from outside the bot's in-memory FSM. Two entry points — the user's "Check payment" button and a public webhook on the existing FastAPI app — funnel into one idempotent `confirm_payment()`; because xPay's callback is unsigned, the webhook is only a trigger and the xPay status endpoint is the sole authority on whether money arrived.

**Tech Stack:** Python 3.12+, aiogram 3 (long polling, in-memory FSM), FastAPI + uvicorn (same process, `asyncio.gather` in `bot/main.py`), SQLAlchemy 2 async, pydantic-settings, httpx. Tests: pytest + pytest-asyncio + respx. Package manager: `uv`.

**Spec:** `docs/superpowers/specs/2026-08-27-xpay-sandbox-integration-design.md`

## Global Constraints

- **Amounts are converted to tyiyn in exactly one place**, `create_payment`, as `int(round(amount_som * 100))`. `100 сом = 10000`.
- **One process-wide `httpx.AsyncClient`.** Never construct a client per request. Constructing one rebuilds the full certifi SSL context; this repo has an OOM history (`railway.toml`, `bot/services/tg.py`) and per-call clients would reintroduce it.
- **The webhook body is never trusted.** Take only `qr_transaction_id` from it; the outcome comes from `GET /qr/dynamic/status/{id}`.
- **The webhook always returns HTTP 200**, including on internal failure, so xPay stops retrying.
- **Never hardcode the service `uuid`.** It is read from `data.service[]` in the login response, matched against `XPAY_MODE`.
- **Do not send `check_url`.** It blocks payment until our server answers `HTTP 201` and buys nothing.
- Sandbox base URL `https://devapi.xpay.kg`; production base URL `https://api.xpay.kg`.
- Token TTL is 30 minutes; refresh 60 seconds before `expires_at`.
- Terminal statuses: `COMPLETED`, `ERROR`, `CANCELED`. Intermediate: `WAITING`, `ACTIVE`, `PROCESSING`.
- Webhook path is `/api/payment/xpay/webhook`, defined once as `WEBHOOK_PATH` in `bot/services/xpay.py`.
- Line length 100 (`ruff`, configured in `pyproject.toml`). Run `uv run ruff check .` before each commit.
- **When a later task appends tests to an existing test file, move that snippet's `import` lines up to the file's import block.** Ruff's default rule set includes `E402` (module-level import not at top of file), which `pyproject.toml` does not ignore, so mid-file imports fail the lint gate. Imports written *inside* a function body are fine and are used deliberately where a test needs a helper from another test module.
- No translation strings change. The existing copy already promises a QR code and an automatic check (`bot/locales/translations.py:15,56`).

---

## File Structure

**Created:**
- `bot/services/xpay.py` — all HTTP contact with xPay. No Telegram or DB knowledge.
- `bot/services/fsm.py` — holds the Dispatcher reference so the web layer can reach FSM storage.
- `tests/conftest.py` — env setup + DB fixture.
- `tests/test_config.py`, `tests/test_xpay.py`, `tests/test_payment_flow.py`, `tests/test_webhook.py`
- `scripts/xpay_smoke.py` — manual sandbox probe (Task 8).

**Modified:**
- `bot/config.py` — xPay settings, mode validation, public base URL resolution.
- `bot/database/models.py` — `Payment` table.
- `bot/database/db.py` — migration lines for the new table's columns.
- `bot/handlers/ad_flow.py` — `confirm_payment()` plus rewiring of `process_payment`, `check_payment_cb`, `process_content_ok`.
- `bot/web/app.py` — webhook route.
- `bot/main.py` — register the dispatcher; close the xPay client on shutdown.
- `pyproject.toml` — `httpx` to main deps; pytest tooling to dev deps.

**Deleted:**
- `bot/services/payment.py` — the mock, replaced by `bot/services/xpay.py`.

`confirm_payment()` lives in `bot/handlers/ad_flow.py`, not in a service module, because it needs the `AdFlow` states defined there. Putting it in a service would make `services → handlers → services` circular. `bot/web/app.py` already imports `_spawn` from `ad_flow` (`bot/web/app.py:21`), so this follows the established direction.

---

### Task 1: Test harness and configuration

**Files:**
- Modify: `pyproject.toml`
- Modify: `bot/config.py:1-20` (settings), `bot/config.py:81-83` (model_config)
- Create: `tests/__init__.py` (empty), `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `config.xpay_client_id: str`, `config.xpay_client_secret: str`, `config.xpay_mode: str`, `config.xpay_base_url: str` (property), `config.resolved_public_base_url: str | None` (property). Module constant `XPAY_BASE_URLS: dict[str, str]`.

- [ ] **Step 1: Add dependencies**

Run:
```bash
uv add httpx
uv add --dev pytest pytest-asyncio respx
```

`httpx` is currently dev-only; the bot needs it at runtime. `respx` mocks httpx transports so no test ever reaches the network.

- [ ] **Step 2: Configure pytest**

Append to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

`asyncio_mode = "auto"` means `async def test_*` runs without an `@pytest.mark.asyncio` decorator on every test.

- [ ] **Step 3: Write `tests/conftest.py`**

Environment must be set *before* any `bot.*` import, because `bot/config.py:86` instantiates `Settings()` at import time and `bot/database/db.py:14` builds the engine at import time. Real environment variables take precedence over `.env`, so this isolates tests from the developer's own `.env`.

```python
import os
import pathlib
import tempfile

# Must run before any `bot.*` import: bot/config.py builds Settings() and
# bot/database/db.py builds the engine at module import time.
_TEST_DB = pathlib.Path(tempfile.gettempdir()) / "poputka_test.db"
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB}"
os.environ["XPAY_CLIENT_ID"] = "test-client-id"
os.environ["XPAY_CLIENT_SECRET"] = "test-client-secret"
os.environ["XPAY_MODE"] = "sandbox"
os.environ.pop("PUBLIC_BASE_URL", None)
os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)

import pytest  # noqa: E402

from bot.database.db import Base, engine  # noqa: E402


@pytest.fixture
async def db():
    """A clean schema per test, on a throwaway sqlite file."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def reset_xpay_state():
    """Clear the module-level token/client cache between tests."""
    import bot.services.xpay as xpay

    xpay._client = None
    xpay._token = None
    xpay._service_uuid = None
    xpay._expires_at = None
    yield
    xpay._client = None
    xpay._token = None
    xpay._service_uuid = None
    xpay._expires_at = None
```

Note: the `reset_xpay_state` fixture imports `bot.services.xpay`, which does not exist until Task 2. Add it now; Task 1's tests will fail to collect otherwise. **Therefore: create a minimal placeholder now** — see Step 4.

- [ ] **Step 4: Create the module-level state placeholder**

Create `bot/services/xpay.py` with only the shared state, so `conftest.py` imports cleanly. Task 2 fills in the rest.

```python
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
```

- [ ] **Step 5: Write the failing config tests**

Create `tests/test_config.py`:

```python
import importlib

import pytest

import bot.config


def _settings(monkeypatch, **env):
    """Build a fresh Settings() with the given environment overrides."""
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    importlib.reload(bot.config)
    return bot.config.config


def test_sandbox_mode_selects_dev_base_url(monkeypatch):
    cfg = _settings(monkeypatch, XPAY_MODE="sandbox")
    assert cfg.xpay_base_url == "https://devapi.xpay.kg"


def test_production_mode_selects_live_base_url(monkeypatch):
    cfg = _settings(monkeypatch, XPAY_MODE="production")
    assert cfg.xpay_base_url == "https://api.xpay.kg"


def test_unknown_mode_is_rejected_at_startup(monkeypatch):
    with pytest.raises(Exception):
        _settings(monkeypatch, XPAY_MODE="sandbx")


def test_public_base_url_is_none_when_nothing_is_set(monkeypatch):
    cfg = _settings(monkeypatch, PUBLIC_BASE_URL=None, RAILWAY_PUBLIC_DOMAIN=None)
    assert cfg.resolved_public_base_url is None


def test_public_base_url_falls_back_to_railway_domain(monkeypatch):
    cfg = _settings(
        monkeypatch, PUBLIC_BASE_URL=None, RAILWAY_PUBLIC_DOMAIN="poputka.up.railway.app"
    )
    assert cfg.resolved_public_base_url == "https://poputka.up.railway.app"


def test_explicit_public_base_url_wins_and_trailing_slash_is_stripped(monkeypatch):
    cfg = _settings(
        monkeypatch,
        PUBLIC_BASE_URL="https://example.kg/",
        RAILWAY_PUBLIC_DOMAIN="ignored.up.railway.app",
    )
    assert cfg.resolved_public_base_url == "https://example.kg"


def test_xpay_api_key_setting_is_gone(monkeypatch):
    cfg = _settings(monkeypatch, XPAY_MODE="sandbox")
    assert not hasattr(cfg, "xpay_api_key")
```

The last test pins the removal: `xpay_api_key` is unused and the wrong shape (this API needs a client id/secret pair, not a single key).

- [ ] **Step 6: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'xpay_base_url'` on most tests.

- [ ] **Step 7: Implement the config changes**

In `bot/config.py`, add above the class:

```python
XPAY_BASE_URLS = {
    "sandbox": "https://devapi.xpay.kg",
    "production": "https://api.xpay.kg",
}
```

Replace the `xpay_api_key: str = ""` line (`bot/config.py:10`) with:

```python
    xpay_client_id: str = ""
    xpay_client_secret: str = ""
    xpay_mode: str = "sandbox"
    public_base_url: str = ""
```

Add to the class body:

```python
    @field_validator("xpay_mode", mode="before")
    @classmethod
    def parse_xpay_mode(cls, v):
        val = str(v or "sandbox").strip().strip("'\"").lower()
        if val not in XPAY_BASE_URLS:
            raise ValueError(
                f"XPAY_MODE must be one of {sorted(XPAY_BASE_URLS)}, got {val!r}"
            )
        return val

    @property
    def xpay_base_url(self) -> str:
        return XPAY_BASE_URLS[self.xpay_mode]

    @property
    def resolved_public_base_url(self) -> str | None:
        """Origin xPay should call back to, or None when we are not reachable.

        None means local development: the caller omits callback_url entirely
        and the flow falls back to the user's "Check payment" button.
        """
        val = self.public_base_url.strip().strip("'\"").rstrip("/")
        if val:
            return val
        domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().strip("'\"")
        return f"https://{domain}" if domain else None
```

`resolved_public_base_url` is a property rather than a `field_validator` deliberately: pydantic does not run validators on unset fields by default, so a validator would never see the `RAILWAY_PUBLIC_DOMAIN` fallback — which is exactly the deployed case.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (7 tests).

- [ ] **Step 9: Add the sandbox credentials to `.env`**

Append to `.env` (values from `docs/XPAY_API.md:61-62`):

```
XPAY_MODE=sandbox
XPAY_CLIENT_ID=fNXw8p5lf5MBBRWl2caUDcgV8mihD2nUpEXfsHBDZtjrDHr6OZbztfb6umGT4pf9lWgjvQkf4e1PW1rybf1FPiEr6yUzT9Aem3VD
XPAY_CLIENT_SECRET=tWUaGGC7kwow42mfVYV13Cn8gPTA9lKt1Pa7gjAPGOgrRThyxA4h29TYOW4zMxpoqNc5bUw7NpU67Pw2UFjNstada6SfDEfK1Aaj
```

`.env` is gitignored — verify with `git check-ignore .env` before proceeding. Do not commit it.

- [ ] **Step 10: Commit**

```bash
uv run ruff check .
git add pyproject.toml uv.lock bot/config.py bot/services/xpay.py tests/
git commit -m "feat: add xPay configuration and test harness"
```

---

### Task 2: xPay authentication and token caching

**Files:**
- Modify: `bot/services/xpay.py`
- Create: `tests/test_xpay.py`

**Interfaces:**
- Consumes: `config.xpay_base_url`, `config.xpay_mode`, `config.xpay_client_id`, `config.xpay_client_secret` (Task 1).
- Produces:
  - `class XPayError(Exception)`
  - `get_client() -> httpx.AsyncClient`
  - `async close_client() -> None`
  - `async _get_auth() -> tuple[str, str]` returning `(access_token, service_uuid)`
  - `_unwrap(path: str, resp: httpx.Response) -> dict`
  - `_post_json(path: str, payload: dict, headers: dict | None = None) -> dict`
  - `TOKEN_REFRESH_MARGIN: timedelta`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_xpay.py`:

```python
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from bot.services import xpay

BASE = "https://devapi.xpay.kg"
LOGIN = f"{BASE}/api/v1/developer/login"
SERVICE_UUID = "485b0981-8e71-41bf-97fd-4e8769e8cd9f"


def login_body(mode="SANDBOX", minutes=30):
    expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_xpay.py -v`
Expected: FAIL — `AttributeError: module 'bot.services.xpay' has no attribute 'XPayError'`.

- [ ] **Step 3: Implement authentication**

Add to `bot/services/xpay.py` (keeping the existing state block from Task 1):

```python
import logging
from datetime import timedelta, timezone

from bot.config import config

logger = logging.getLogger(__name__)

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
    now = datetime.now(timezone.utc)
    if not raw:
        return now + FALLBACK_TOKEN_TTL
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        logger.warning(f"Unparseable xPay expires_at {raw!r}; assuming short TTL")
        return now + FALLBACK_TOKEN_TTL
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
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
        now = datetime.now(timezone.utc)
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_xpay.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add bot/services/xpay.py tests/test_xpay.py
git commit -m "feat: xPay authentication with token caching"
```

---

### Task 3: QR creation and status polling

**Files:**
- Modify: `bot/services/xpay.py`
- Modify: `tests/test_xpay.py`

**Interfaces:**
- Consumes: `_get_auth()`, `_post_json()`, `_unwrap()`, `XPayError`, `get_client()` (Task 2).
- Produces:
  - `@dataclass(frozen=True) class PaymentQR` with fields `qr_transaction_id: str`, `qr_code: str`, `qr_image: str`
  - `async create_payment(user_id: int, amount_som: float) -> PaymentQR`
  - `async get_payment_status(qr_transaction_id: str) -> str` (uppercased `pay_status`)
  - `SERVICE_NAME: str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_xpay.py`:

```python
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
```

Add to the imports at the top of `tests/test_xpay.py`:

```python
import json

from bot.config import config
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_xpay.py -v`
Expected: FAIL — `AttributeError: module 'bot.services.xpay' has no attribute 'create_payment'`. The Task 2 tests still pass.

- [ ] **Step 3: Implement QR creation and status polling**

Add to `bot/services/xpay.py`; add `from dataclasses import dataclass` to the imports.

```python
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
        "amount": int(round(amount_som * 100)),
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_xpay.py -v`
Expected: PASS (16 tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add bot/services/xpay.py tests/test_xpay.py
git commit -m "feat: xPay QR creation and status polling"
```

---

### Task 4: Payment table

**Files:**
- Modify: `bot/database/models.py` (append after `Campaign`)
- Modify: `bot/database/db.py:26-38` (the `migrations` list)
- Create: `tests/test_payment_flow.py`

**Interfaces:**
- Consumes: `Base` from `bot/database/db.py`.
- Produces: `Payment` model with columns `id, user_id, qr_transaction_id, amount, status, campaign_id, created_at, updated_at`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_payment_flow.py`:

```python
from sqlalchemy import select

from bot.database.db import AsyncSessionLocal
from bot.database.models import Payment


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_payment_flow.py -v`
Expected: FAIL — `ImportError: cannot import name 'Payment'`.

- [ ] **Step 3: Add the model**

Append to `bot/database/models.py`:

```python
class Payment(Base):
    """One xPay payment attempt.

    Required rather than optional: the webhook arrives out of band with no
    FSM context, so qr_transaction_id must be resolvable to a user from
    durable storage.
    """

    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    qr_transaction_id = Column(String, unique=True, index=True)
    amount = Column(Float, default=0.0)  # som, what we charged
    status = Column(String, default="WAITING")
    campaign_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
```

All imports it needs (`BigInteger`, `Column`, `DateTime`, `Float`, `Integer`, `String`, `datetime`) are already present at the top of the file.

- [ ] **Step 4: Add migration lines for the existing deployed database**

`init_db()` calls `create_all`, which creates the whole `payments` table on a database that has never seen it — so no migration is needed for the initial rollout. Add the `ALTER TABLE` lines anyway, matching the file's established pattern, so a later column addition has an obvious home. Append to the `migrations` list in `bot/database/db.py`:

```python
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS campaign_id INTEGER",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_payment_flow.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add bot/database/models.py bot/database/db.py tests/test_payment_flow.py
git commit -m "feat: add Payment table"
```

---

### Task 5: FSM storage access from outside the polling loop

**Files:**
- Create: `bot/services/fsm.py`
- Modify: `bot/main.py:38` (after `dp = Dispatcher()`), `bot/main.py:66-69` (the `finally` block)
- Modify: `tests/test_payment_flow.py`

**Interfaces:**
- Consumes: `get_bot()` from `bot/services/tg.py`; `close_client()` from `bot/services/xpay.py` (Task 2).
- Produces:
  - `set_dispatcher(dp: Dispatcher) -> None`
  - `get_fsm_context(user_id: int) -> FSMContext | None` — `None` when no dispatcher is registered.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_payment_flow.py`:

```python
from aiogram import Dispatcher

from bot.handlers.ad_flow import AdFlow
from bot.services import fsm


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_payment_flow.py -v`
Expected: FAIL — `ImportError: cannot import name 'fsm' from 'bot.services'`.

- [ ] **Step 3: Implement the accessor**

Create `bot/services/fsm.py`:

```python
"""Access to the dispatcher's FSM storage from outside the polling loop.

bot/main.py gathers dp.start_polling() and the uvicorn server on a single
event loop in a single process, so the web layer can move a user's FSM
state directly. This is a shared reference, not IPC.

Storage is in-memory, so state does not survive a restart. The Payment row
is the durable record; see the spec's "Known limitations".
"""

import logging

from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from bot.services.tg import get_bot

logger = logging.getLogger(__name__)

_dispatcher: Dispatcher | None = None


def set_dispatcher(dp: Dispatcher) -> None:
    global _dispatcher
    _dispatcher = dp


def get_fsm_context(user_id: int) -> FSMContext | None:
    """FSM context for a user's private chat with the bot.

    Returns None when no dispatcher has been registered yet, which happens
    only in tests and during early startup.
    """
    if _dispatcher is None:
        logger.warning("FSM context requested before the dispatcher was registered")
        return None
    bot = get_bot()
    # The bot only ever talks to users in private chats, where chat_id
    # equals user_id.
    return FSMContext(
        storage=_dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id),
    )
```

- [ ] **Step 4: Wire it into `bot/main.py`**

Add to the imports:

```python
from bot.services.fsm import set_dispatcher
from bot.services.xpay import close_client as close_xpay_client
```

Immediately after `dp = Dispatcher()` (`bot/main.py:38`):

```python
    # The xPay webhook runs in the web layer and needs to move a paying
    # user's FSM state, so the dispatcher must be reachable from there.
    set_dispatcher(dp)
```

Change the `finally` block (`bot/main.py:66-69`) to also close the xPay client:

```python
    finally:
        await close_xpay_client()
        await close_bot()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS (all tests, 27 total: 7 config + 16 xpay + 4 payment flow).

- [ ] **Step 6: Verify the app still starts**

Run: `uv run python -c "import bot.main"`
Expected: no output, exit code 0. This catches a circular import between `bot.services.fsm`, `bot.services.tg`, and `bot.main`.

- [ ] **Step 7: Commit**

```bash
uv run ruff check .
git add bot/services/fsm.py bot/main.py tests/test_payment_flow.py
git commit -m "feat: expose FSM storage to the web layer"
```

---

### Task 6: Wire the real payment into the ad flow

**Files:**
- Modify: `bot/handlers/ad_flow.py:20` (import), `:373-395` (`process_payment`), `:398-411` (`check_payment_cb`), `:481-499` (`process_content_ok`)
- Delete: `bot/services/payment.py`
- Modify: `tests/test_payment_flow.py`

**Interfaces:**
- Consumes: `create_payment()`, `get_payment_status()`, `XPayError` (Tasks 2–3); `Payment` (Task 4); `get_fsm_context()` (Task 5).
- Produces: `async confirm_payment(payment_id: int) -> bool` in `bot/handlers/ad_flow.py`, imported by `bot/web/app.py` in Task 7.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_payment_flow.py`:

```python
import httpx
import respx

from bot.database.models import User
from bot.handlers.ad_flow import confirm_payment

BASE = "https://devapi.xpay.kg"
LOGIN = f"{BASE}/api/v1/developer/login"


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_payment_flow.py -v`
Expected: FAIL — `ImportError: cannot import name 'confirm_payment' from 'bot.handlers.ad_flow'`.

- [ ] **Step 3: Replace the import in `bot/handlers/ad_flow.py`**

Replace line 20:

```python
from bot.services.payment import check_xpay_payment, generate_xpay_link
```

with:

```python
from bot.services.fsm import get_fsm_context
from bot.services.xpay import XPayError, create_payment, get_payment_status
```

Add `Payment` to the models import on line 16:

```python
from bot.database.models import Campaign, Payment, User
```

Add `from datetime import datetime` to the imports at the top.

- [ ] **Step 4: Add `_notify_paid` and `confirm_payment`**

Insert into `bot/handlers/ad_flow.py`, above `process_payment`:

```python
def _notify_paid(user_id: int, lang: str) -> None:
    """Send the payment-confirmed message without blocking the caller.

    Split out so tests can replace it, and so a webhook is not held open
    waiting on the Telegram API.
    """
    _spawn(get_bot().send_message(user_id, get_text(lang, "payment_confirmed")))


async def confirm_payment(payment_id: int) -> bool:
    """Settle one payment. Shared by the check button and the xPay webhook.

    The webhook payload is unsigned, so it is only ever a trigger: the xPay
    status endpoint is the sole authority on whether money arrived.
    Idempotent, so duplicate deliveries and button/webhook races are safe.
    """
    async with AsyncSessionLocal() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            return False
        if payment.status == "COMPLETED":
            return True
        qr_transaction_id = payment.qr_transaction_id
        user_id = payment.user_id

    try:
        pay_status = await get_payment_status(qr_transaction_id)
    except XPayError as e:
        logger.warning(f"xPay status check failed for {qr_transaction_id}: {e}")
        return False

    async with AsyncSessionLocal() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            return False
        if payment.status == "COMPLETED":
            # A concurrent caller (button vs webhook) already settled it.
            return True

        payment.status = pay_status
        payment.updated_at = datetime.utcnow()
        await session.commit()

        if pay_status != "COMPLETED":
            return False

        lang = await get_user_lang(user_id, session)

    state = get_fsm_context(user_id)
    if state is not None:
        await state.set_state(AdFlow.content)

    _notify_paid(user_id, lang)
    logger.info(f"Payment {payment_id} confirmed for user {user_id}")
    return True
```

- [ ] **Step 5: Rewrite `process_payment`**

Replace the body of `process_payment` (`bot/handlers/ad_flow.py:373-395`):

```python
@router.callback_query(AdFlow.confirm_payment, F.data == "pay_yes")
async def process_payment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    count = data["count"]
    lang = data.get("lang", "ky")
    price = count * await get_price_per_ad()

    try:
        qr = await create_payment(callback.from_user.id, price)
    except XPayError as e:
        logger.error(f"Failed to create xPay payment for {callback.from_user.id}: {e}")
        await callback.answer(get_text(lang, "payment_not_found"), show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        payment = Payment(
            user_id=callback.from_user.id,
            qr_transaction_id=qr.qr_transaction_id,
            amount=price,
            status="WAITING",
        )
        session.add(payment)
        await session.commit()
        payment_db_id = payment.id

    await state.update_data(payment_db_id=payment_db_id, price=price)

    msg_text = get_text(lang, "payment_info", price=price, link=qr.qr_code)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_text(lang, "check_payment"), callback_data="check_pay"
                )
            ]
        ]
    )

    # The summary message is a plain text message, so it cannot be edited
    # into a photo; delete it and send the QR image instead.
    await callback.message.delete()
    if qr.qr_image:
        await callback.message.answer_photo(
            qr.qr_image, caption=msg_text, reply_markup=markup
        )
    else:
        await callback.message.answer(msg_text, reply_markup=markup)

    await state.set_state(AdFlow.waiting_payment)
```

Staying in `AdFlow.confirm_payment` on error is deliberate: the "Pay" button remains live, so the user can simply press it again.

- [ ] **Step 6: Rewrite `check_payment_cb`**

Replace `check_payment_cb` (`bot/handlers/ad_flow.py:398-411`):

```python
@router.callback_query(AdFlow.waiting_payment, F.data == "check_pay")
async def check_payment_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ky")
    payment_db_id = data.get("payment_db_id")

    if payment_db_id is None or not await confirm_payment(payment_db_id):
        await callback.answer(get_text(lang, "payment_not_found"), show_alert=True)
        return

    # confirm_payment already set the state and sent payment_confirmed.
    await callback.message.delete()
```

- [ ] **Step 7: Link the payment to the campaign in `process_content_ok`**

In `process_content_ok` (`bot/handlers/ad_flow.py:481`), inside the existing `async with AsyncSessionLocal() as session:` block, after `await session.commit()`:

```python
        payment_db_id = data.get("payment_db_id")
        if payment_db_id:
            payment = await session.get(Payment, payment_db_id)
            if payment:
                payment.campaign_id = campaign.id
                payment.updated_at = datetime.utcnow()
                await session.commit()
```

- [ ] **Step 8: Delete the mock service**

```bash
git rm bot/services/payment.py
```

Confirm nothing still imports it:

```bash
grep -rn "services.payment\|check_xpay_payment\|generate_xpay_link" bot/
```
Expected: no output.

- [ ] **Step 9: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS (32 tests).

- [ ] **Step 10: Commit**

```bash
uv run ruff check .
git add -A bot/handlers/ad_flow.py bot/services/payment.py tests/test_payment_flow.py
git commit -m "feat: use real xPay payments in the ad flow"
```

---

### Task 7: Webhook endpoint

**Files:**
- Modify: `bot/web/app.py` (imports near line 21; new route after the `/health` handler at line 89)
- Create: `tests/test_webhook.py`

**Interfaces:**
- Consumes: `confirm_payment()` (Task 6), `Payment` (Task 4), `WEBHOOK_PATH` (Task 1).
- Produces: `POST /api/payment/xpay/webhook`, always `200 {"status": "ok"}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_webhook.py`:

Note the deliberate absence of `fastapi.testclient.TestClient`: it drives the app from a second thread with its own event loop, while the SQLAlchemy async engine and its connection pool are bound to the loop the test runs on. Calling the route coroutine directly with a stub request keeps everything on one loop, keeps `respx` in effect, and tests the handler logic — which is all the ASGI layer would add.

```python
import httpx
import pytest
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
```

`pytest` is imported for the `db` fixture's sake only if you add fixtures; if ruff reports it as unused, drop the import.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_webhook.py -v`
Expected: FAIL at collection — `ImportError: cannot import name 'xpay_webhook' from 'bot.web.app'`.

- [ ] **Step 3: Implement the route**

Add to the imports in `bot/web/app.py`:

```python
from bot.database.models import Campaign, GroupPost, Payment, User
from bot.handlers.ad_flow import _spawn, confirm_payment
from bot.services.xpay import WEBHOOK_PATH
```

Add the route after the `/health` handler (`bot/web/app.py:89-91`):

```python
def _extract_qr_transaction_id(body) -> str:
    """Pull the transaction id from either a flat or a `data`-wrapped body."""
    if not isinstance(body, dict):
        return ""
    candidate = body.get("qr_transaction_id")
    if not candidate and isinstance(body.get("data"), dict):
        candidate = body["data"].get("qr_transaction_id")
    return str(candidate or "").strip()


@app.post(WEBHOOK_PATH)
async def xpay_webhook(request: Request):
    """Public, unauthenticated: xPay cannot present an admin session cookie.

    The callback is unsigned, so nothing in the body is trusted. We take
    only the transaction id and then ask the xPay status endpoint what
    actually happened. A forged request can at worst cost us one status
    call for a transaction that already exists.

    Always returns 200 - including on internal failure - so xPay stops
    retrying. The user's "Check payment" button remains the fallback.
    """
    try:
        body = await request.json()
    except Exception:
        return {"status": "ok"}

    qr_transaction_id = _extract_qr_transaction_id(body)
    if not qr_transaction_id:
        return {"status": "ok"}

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Payment).where(
                    Payment.qr_transaction_id == qr_transaction_id
                )
            )
            payment = result.scalar_one_or_none()

        if payment is None:
            logger.info(f"xPay webhook for unknown transaction {qr_transaction_id}")
            return {"status": "ok"}

        await confirm_payment(payment.id)
    except Exception as e:
        logger.warning(f"xPay webhook failed for {qr_transaction_id}: {e}")

    return {"status": "ok"}
```

`select` and `Request` are already imported (`bot/web/app.py:12,15`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS (40 tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add bot/web/app.py tests/test_webhook.py
git commit -m "feat: add xPay payment webhook"
```

---

### Task 8: Sandbox verification against the live API

Everything to this point is mocked. This task proves the integration against the real sandbox before anything is deployed.

**Files:**
- Create: `scripts/xpay_smoke.py`
- Modify: `README.md`

- [ ] **Step 1: Write the smoke script**

Create `scripts/xpay_smoke.py`:

```python
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
```

- [ ] **Step 2: Verify login against the live sandbox**

Run: `uv run python scripts/xpay_smoke.py`

Expected: `mode=sandbox base_url=https://devapi.xpay.kg`, a real token prefix, and a `service_uuid`. This confirms the credentials in `docs/XPAY_API.md` still work. If login fails with `Invalid credentials`, stop — the documented sandbox keys have been rotated and you need new ones before continuing.

- [ ] **Step 3: Verify QR creation and the tyiyn conversion**

From the same output: a `qr_transaction_id`, a `devpay.xpay.kg` link, and a `devimage.xpay.kg` PNG URL, with `status=WAITING` (or `ACTIVE`).

Open the `qr_image` URL in a browser and confirm the QR renders and the amount reads **1 сом**, not 100. This is the end-to-end check that `int(round(amount_som * 100))` is right in the direction we think.

- [ ] **Step 4: Verify the full bot flow locally**

1. `uv run python -m bot.main`
2. In Telegram, walk the ad flow through to "Pay".
3. Confirm the bot sends a **QR image** with the price and a working `devpay.xpay.kg` link.
4. Open `https://sandbox.xpay.kg`, find that transaction, mark it paid.
5. Press "Check payment" (Проверить оплату).
6. Confirm the bot replies with `payment_confirmed` and accepts your ad content next.
7. Confirm in the database that the row settled:
   ```bash
   uv run python -c "
   import asyncio, sqlite3
   rows = sqlite3.connect('bot.db').execute(
       'select id,user_id,qr_transaction_id,amount,status,campaign_id from payments'
   ).fetchall()
   print(*rows, sep='\n')"
   ```
   Expected: `status=COMPLETED` and, after the campaign is created, a non-null `campaign_id`.

No tunnel and no public URL are required for any of this.

- [ ] **Step 5: Verify the webhook handler locally**

With the bot still running and a **fresh, genuinely paid** sandbox transaction id:

```bash
curl -s -X POST localhost:8000/api/payment/xpay/webhook \
  -H 'Content-Type: application/json' \
  -d '{"qr_transaction_id":"<paid_id>"}'
```
Expected: `{"status":"ok"}`, the bot messages the user unprompted, and the row flips to `COMPLETED`.

Then the negative check, with an **unpaid** transaction id:

```bash
curl -s -X POST localhost:8000/api/payment/xpay/webhook \
  -H 'Content-Type: application/json' \
  -d '{"qr_transaction_id":"<unpaid_id>","pay_status":"COMPLETED"}'
```
Expected: `{"status":"ok"}` and the row **still** `WAITING`. This is the one that proves a forged callback cannot buy advertising for free.

- [ ] **Step 6: Document the environment variables**

Add to `README.md`:

```markdown
## Payments (xPay)

| Variable | Required | Notes |
| :--- | :--- | :--- |
| `XPAY_CLIENT_ID` | yes | From lk.xpay.kg, or the sandbox pair in `docs/XPAY_API.md` |
| `XPAY_CLIENT_SECRET` | yes | Same |
| `XPAY_MODE` | no | `sandbox` (default) or `production` |
| `PUBLIC_BASE_URL` | no | Origin xPay calls back to. Falls back to `https://$RAILWAY_PUBLIC_DOMAIN`; unset locally, which disables the callback and relies on the check button. |

Going live: obtain production keys from lk.xpay.kg, set `XPAY_MODE=production`.
No code change is required — the merchant service uuid is resolved from the
login response by mode.

Manual sandbox probe: `uv run python scripts/xpay_smoke.py`.
Test payments are settled at https://sandbox.xpay.kg.
```

- [ ] **Step 7: Commit**

```bash
uv run ruff check .
git add scripts/xpay_smoke.py README.md
git commit -m "docs: xPay sandbox smoke script and environment reference"
```

- [ ] **Step 8: Set the Railway environment variables**

Before deploying, set `XPAY_CLIENT_ID`, `XPAY_CLIENT_SECRET`, and `XPAY_MODE=sandbox` in the Railway service. Leave `PUBLIC_BASE_URL` unset so the Railway-provided domain is used automatically.

- [ ] **Step 9: Post-deploy verification (the only step that requires deploying)**

Repeat Step 4 against the deployed bot, but **do not press "Check payment"**. After marking the transaction paid at `https://sandbox.xpay.kg`, the bot should send the confirmation on its own within a few seconds, driven by the callback.

If it does not, check the Railway logs for a request to `/api/payment/xpay/webhook`. No request means `callback_url` was not sent — confirm `RAILWAY_PUBLIC_DOMAIN` is present in the environment. The flow still works via the button in the meantime; the webhook is an accelerator, not a dependency.

---

## Self-Review Notes

Spec coverage check — every spec section maps to a task:

| Spec section | Task |
| :--- | :--- |
| Configuration, mode switch, `public_base_url` resolution, `xpay_api_key` removal | 1 |
| `bot/services/xpay.py`, client singleton, token cache, service uuid by mode | 2 |
| `create_payment` / `get_payment_status`, tyiyn conversion, no `check_url`, conditional `callback_url` | 3 |
| `Payment` table | 4 |
| FSM reachable from the web layer | 5 |
| `confirm_payment` idempotency, handler rewiring, `campaign_id` linkage | 6 |
| Webhook, untrusted body, always-200, no auth dependency | 7 |
| Verification steps 1–5, go-live notes | 8 |

Out-of-scope items from the spec (static QR, transaction history, `check_url`, `return_url`, restart recovery, refunds) have no tasks, as intended.
