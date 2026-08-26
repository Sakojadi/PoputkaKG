# xPay Sandbox Integration — Design

**Date:** 2026-08-27
**Status:** Approved for planning
**Replaces:** the mock payment service in `bot/services/payment.py`

## Goal

Replace the fake payment stub with a real xPay integration running against the
sandbox environment, structured so that going live is an environment-variable
change rather than a code change.

Today `generate_xpay_link()` returns a fabricated URL and `check_xpay_payment()`
returns `True` unconditionally (`bot/services/payment.py:4`). Every ad campaign is
therefore created without any money changing hands.

## Scope

In scope:

- An xPay API client covering login, dynamic QR creation, and status polling.
- A `Payment` table recording every payment attempt.
- A public webhook endpoint on the existing FastAPI app.
- Sandbox/production switching via configuration.

Explicitly out of scope:

- Static QR codes (§4 of the API doc). This product bills a per-user amount for a
  specific order; static QR solves a physical-till problem we do not have.
- The transaction-history endpoint (§3 of the API doc).
- `check_url` pre-authorisation. It blocks the payment until our server answers
  `HTTP 201`, adding a failure mode and buying nothing.
- `return_url`. A Telegram deep link back to the bot is a nice touch but is not
  required for correctness.
- Recovering an in-flight flow after a process restart. See "Known limitations".
- Refunds, and reconciling xPay's ledger against ours.

## Environment asymmetry

This constraint shapes the whole design:

| Direction | Local | Railway |
| :--- | :--- | :--- |
| Bot → Telegram (long polling) | works | works |
| Bot → xPay API (outbound HTTPS) | works | works |
| xPay → our `callback_url` | impossible, no public URL | works |

Outbound calls behave identically in both environments; inbound callbacks only
work when deployed. Therefore **polling is the source of truth and the webhook is
an accelerator**. The flow must be correct even if a callback never arrives.

## Architecture

### Configuration

`bot/config.py` gains:

| Setting | Env var | Default | Notes |
| :--- | :--- | :--- | :--- |
| `xpay_client_id` | `XPAY_CLIENT_ID` | `""` | Sandbox value from the API doc |
| `xpay_client_secret` | `XPAY_CLIENT_SECRET` | `""` | Sandbox value from the API doc |
| `xpay_mode` | `XPAY_MODE` | `"sandbox"` | `sandbox` or `production` |
| `public_base_url` | `PUBLIC_BASE_URL` | resolved, see below | Origin xPay calls back to |

`xpay_api_key` is removed. It is unused and the wrong shape — this API
authenticates with a client id/secret pair, not a single key.

Base URL follows the mode: `sandbox` → `https://devapi.xpay.kg`,
`production` → `https://api.xpay.kg`. A validator rejects any other value at
startup rather than letting a typo silently pick a default.

`public_base_url` resolves in order: the explicit env var, else
`https://$RAILWAY_PUBLIC_DOMAIN` if Railway set it, else `None`. **When it is
`None` we omit `callback_url` from the QR request entirely.** This one rule is
what lets the same code path run locally and deployed.

The service `uuid` required by `qr/get` is never hardcoded. It is read from
`data.service[]` in the login response, selecting the entry whose `mode` matches
`xpay_mode`. If no entry matches, the client raises — a sandbox key used in
production mode fails loudly instead of appearing to take money.

### `bot/services/xpay.py`

Replaces `payment.py`. It owns **one module-level `httpx.AsyncClient`**, following
the singleton pattern already established in `bot/services/tg.py` and for the same
reason recorded there: constructing a client rebuilds the full certifi SSL context,
and doing that per request leaks memory faster than the GC returns it. This
codebase has an OOM history; a per-call client would reintroduce it.

`httpx` moves from the dev dependency group into the main dependencies.

Public surface:

- `create_payment(user_id: int, amount_som: float) -> PaymentQR`
  Calls `POST /api/v1/developer/qr/get`. Returns `qr_transaction_id`, `qr_code`
  (the payment page link) and `qr_image` (PNG URL).
- `get_payment_status(qr_transaction_id: str) -> str`
  Calls `GET /api/v1/developer/qr/dynamic/status/{id}`, returns the raw
  `pay_status` string.

Internal:

- `_get_token()` caches the `access_token` and the resolved `service_uuid`
  together, refreshing 60 seconds before `expires_at`. An `asyncio.Lock` guards
  the refresh so concurrent users cannot stampede the login endpoint.

**Amounts.** The API takes tyiyn. Conversion is `int(round(amount_som * 100))`,
done in exactly one place, in `create_payment`. Status responses return sums in
som, sometimes as strings (`"235.00000000"`), sometimes as numbers — the client
does not parse them, because our own `Payment.amount` is authoritative for what we
charged.

**Failures.** Every request carries an explicit timeout. Network errors and
non-`Success` responses raise `XPayError`; callers surface a friendly message and
leave the user's state untouched rather than advancing the flow.

### Data model

New table, in `bot/database/models.py`:

```
Payment
  id                 Integer, pk
  user_id            BigInteger
  qr_transaction_id  String, unique, indexed
  amount             Float          # som, what we charged
  status             String         # WAITING | COMPLETED | ERROR | CANCELED
  campaign_id        Integer, nullable
  created_at         DateTime
  updated_at         DateTime
```

The table is required rather than optional: a webhook arrives out of band, with no
FSM context to read, so `qr_transaction_id → user_id` must be resolvable from
durable storage.

`campaign_id` is filled in when the campaign is created at the end of the flow,
linking money to what it bought.

### Webhook

`POST /api/payment/xpay/webhook` on the existing FastAPI app, with **no**
`require_admin` dependency. Auth there is a per-route dependency
(`bot/web/app.py:70`), not global middleware, so a public route is a clean
addition.

The API documentation specifies no signature on the callback, so the endpoint
treats the request as **a trigger, never as a fact**:

1. Read `qr_transaction_id` from the body.
2. Look up the `Payment` row. Unknown id → return `200`, ignore.
3. Call `get_payment_status()` ourselves. **Only that response decides the
   outcome.** The request body's own status claim is never trusted.
4. On `COMPLETED`, run the shared confirmation path.
5. Always return `200`, so xPay stops retrying even on our internal errors.

Because the payload is untrusted, a forged request can at worst cause us to make
one status call for a transaction that exists.

### Confirmation path

The webhook and the "Check payment" button converge on one function:

```
confirm_payment(payment) -> bool
    if payment.status == "COMPLETED": return True      # idempotent
    status = await get_payment_status(payment.qr_transaction_id)
    if status != "COMPLETED": return False
    mark row COMPLETED
    move that user's FSM to AdFlow.content
    send the payment_confirmed message
    return True
```

Idempotency is what makes webhook-then-button, button-then-webhook, and duplicate
webhook deliveries all safe.

Setting FSM state from the web layer requires the dispatcher's storage to be
reachable outside the polling loop, exposed the way `get_bot()` exposes the bot
(`bot/services/tg.py`). Both run in the same process — `bot/main.py` gathers
`start_polling` and `server.serve()` on one loop — so this is a reference, not
inter-process communication. The web layer already imports from the bot layer
(`bot/web/app.py:21`), so no new architectural boundary is crossed.

In aiogram terms the webhook constructs
`FSMContext(storage=dp.storage, key=StorageKey(bot_id, chat_id, user_id))` and
calls `set_state(AdFlow.content)`.

### Handler changes

`bot/handlers/ad_flow.py`:

- `process_payment` (`pay_yes`, line 373) calls `create_payment()`, writes the
  `Payment` row, stores `qr_transaction_id` in FSM state, and sends the `qr_image`
  as a photo with the existing `payment_info` caption and "Check payment" button.
  On `XPayError` it shows an error and stays in `confirm_payment` so the user can
  retry.
- `check_payment_cb` (`check_pay`, line 398) loads the `Payment` row and calls
  `confirm_payment()`; on `False` it keeps the existing `payment_not_found` alert.
- `process_content_ok` sets `Payment.campaign_id` on the new campaign.

No translation strings change. The existing copy already promises a QR code and an
automatic check (`bot/locales/translations.py:15,56`), which is what this delivers.

## Verification

Steps 1–4 run locally, before anything is deployed.

1. **Login.** A throwaway script authenticates against the sandbox and asserts
   `mode == "SANDBOX"` with a `service_uuid` resolved. Confirms the documented
   test credentials still work.
2. **QR creation.** Create a 1.00 KGS payment; assert `amount=100` was sent
   (tyiyn conversion) and that a `qr_transaction_id` and `qr_image` come back.
3. **Full bot flow.** Run the bot locally, walk the ad flow to `pay_yes`, receive
   a real QR image and `devpay.xpay.kg` link in Telegram. Open
   `https://sandbox.xpay.kg`, mark that transaction paid, press "Check payment",
   and confirm the status returns `COMPLETED` and the flow advances to content
   entry. Needs no tunnel and no public URL.
4. **Webhook handler.** `POST` a handcrafted callback body to
   `localhost:8000/api/payment/xpay/webhook` carrying a real sandbox transaction
   id. This exercises the whole handler including the verify-via-API step and the
   FSM transition. Also assert that an unknown id returns `200` and changes
   nothing, and that a body claiming `COMPLETED` for an unpaid transaction does
   **not** confirm it.
5. **After deploy only.** Repeat step 3 on Railway with `callback_url` now live
   and confirm the flow advances *without* pressing the button.

Only the network hop from xPay's servers to ours is untestable before deploying,
and the flow is correct without it.

## Known limitations

FSM storage is in-memory, so a process restart — including the OOM bounce that
`railway.toml` restarts on — still drops an in-flight flow. With the `Payment`
row the payment is now recorded rather than lost, which is strictly better than
today, but the user would need to contact an admin. Automatic recovery is
deliberately deferred.

Production credentials from `lk.xpay.kg` are not yet available. Obtaining them and
flipping `XPAY_MODE` is a separate go-live step; no code change is required.
