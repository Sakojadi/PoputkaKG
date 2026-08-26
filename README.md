# PoputkaKG

## Payments (xPay)

| Variable | Required | Notes |
| :--- | :--- | :--- |
| `XPAY_CLIENT_ID` | yes | From lk.xpay.kg, or the sandbox pair in `docs/XPAY_API.md` |
| `XPAY_CLIENT_SECRET` | yes | Same |
| `XPAY_MODE` | no | `sandbox` (default) or `production` |
| `PUBLIC_BASE_URL` | no | Origin xPay calls back to. Falls back to `https://$RAILWAY_PUBLIC_DOMAIN`; unset locally, which disables the callback and relies on the check button. |

`XPAY_CLIENT_ID` and `XPAY_CLIENT_SECRET` are required for any payment to work
at all; the other two are optional and have safe defaults. `PUBLIC_BASE_URL`
is the single most important operational knob here: when it (and
`RAILWAY_PUBLIC_DOMAIN`) are both unset — the normal case for local
development — `create_payment` omits `callback_url` entirely, so xPay never
tries to reach back to us. In that mode the payment still completes; the
user just has to press the bot's "Check payment" (Проверить оплату) button
instead of getting an unprompted push. On Railway, `RAILWAY_PUBLIC_DOMAIN`
is set by the platform automatically, so the callback is wired up with no
extra configuration, and the same button remains a manual fallback if the
webhook is ever delayed or missed.

Going live: obtain production keys from lk.xpay.kg, set
`XPAY_MODE=production`. No code change is required — the merchant service
uuid is resolved from the login response by mode.

Manual sandbox probe: `uv run python -m scripts.xpay_smoke`.
Test payments are settled at https://sandbox.xpay.kg.
