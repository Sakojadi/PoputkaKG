# PoputkaKG

## Payments

| Variable | Required | Notes |
| :--- | :--- | :--- |
| `PAYMENT_PROVIDER` | no | `mock` (default) or `xpay` |
| `XPAY_CLIENT_ID` | only for `xpay` | From lk.xpay.kg, or the sandbox pair in `docs/XPAY_API.md` |
| `XPAY_CLIENT_SECRET` | only for `xpay` | Same |
| `XPAY_MODE` | no | `sandbox` (default) or `production` |
| `PUBLIC_BASE_URL` | no | Origin xPay calls back to. Falls back to `https://$RAILWAY_PUBLIC_DOMAIN`; unset locally, which disables the callback and relies on the check button. |

### `PAYMENT_PROVIDER=mock` — the current default

The mock provider takes no money and verifies nothing. It hands the user a
placeholder `https://pay.xpay.kg/mock/...` link and reports every payment as
`COMPLETED`, so pressing "Check payment" always advances the flow. The xPay
credentials are not read at all, and the callback webhook is inert.

Everything else behaves normally: a `payments` row is still written for each
attempt and still linked to the campaign it bought, so the records are real
even though the money is not.

**Anyone who presses "Pay" gets their ads for free.** That is the intended
behaviour while the xPay integration is parked — but it means flipping to
`xpay` is the only thing standing between this deployment and free ads.

### `PAYMENT_PROVIDER=xpay` — the real integration

Set `PAYMENT_PROVIDER=xpay` plus `XPAY_CLIENT_ID` and `XPAY_CLIENT_SECRET`.
Nothing else changes; the code path is unchanged from the sandbox integration
and is covered by the existing tests. Before switching it on, work through
`docs/NEXT_STEPS.md` — in particular §1.1, verifying that a 1 сом QR renders
as 1 сом and not 100.

`XPAY_CLIENT_ID` and `XPAY_CLIENT_SECRET` are required for any real payment to
work at all; the other two are optional and have safe defaults. `PUBLIC_BASE_URL`
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

## Group moderation

The bot only talks to people in **private chats**. In the group (`GROUP_ID`) it
never replies; it only moderates. It needs the **Delete messages** and
**Ban users** admin rights there.

Manage it at `/admin/moderation` in the web panel:

| Mode | What happens |
| :--- | :--- |
| `off` | No checks. |
| `log` (default) | Nothing is deleted. Each flagged message is recorded in the events table with the action the bot *would* take. Review this for a few days first. |
| `enforce` | The message is deleted and the ladder below is applied. |

Ladder (only `enforce` actions count as strikes; group admins are never checked):

| Violation | 1st | 2nd | 3rd |
| :--- | :--- | :--- | :--- |
| **Spam** (porn/18+ phrases, forwarded message with link buttons) | mute 24 h | ban | |
| **Rule violation** (profanity, drugs, gambling, outside links — editable word list) | warning | mute 24 h (within 24 h) | ban (within 24 h) |

Edited messages are checked too. Photos are not scanned (text, captions and
button labels only).

`ADMIN_IDS` is no longer used; remove it from your environment.
