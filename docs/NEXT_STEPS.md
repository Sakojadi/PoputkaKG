# What to do next

**As of:** 2026-08-27 · branch `fix-oom-crash` · 22 commits ahead of `main` · 45 tests passing

This is the action list distilled from the full review in
[`docs/reviews/2026-08-27-24h-review.md`](reviews/2026-08-27-24h-review.md) (26 findings).
Read that file for the reasoning behind any item; this file is just the order of work.

I independently re-verified every item in §2 against the actual code rather than taking the
review's word for it. Those five are real.

---

## 1. Blocked on you — nobody else can do these

### 1.1 Verify the QR amount before a single real payment ⚠️ highest stakes

The xPay API takes amounts in **tyiyn** (1 сом = 100), and we send that correctly. But the QR's
EMV tag 54 carries the tyiyn integer verbatim: a 1.00 сом request produces `5403100`. Under a
strict EMVCo reading tag 54 is the *major* unit — which would render as **100 KGS, a 100×
overcharge**. Under a minor-unit reading it is correct.

This cannot be settled from code. The status endpoint omits amount fields while a payment is
`WAITING`, so there is nothing to cross-check until someone pays one.

```bash
uv run python -m scripts.xpay_smoke
```

Open the printed `qr_image` URL. **Confirm it reads 1 сом, not 100.** If it reads 100, the fix is
one line in `bot/services/xpay.py` (`create_payment`, the `"amount"` key) — and our own
`Payment.amount` stays authoritative in som either way, so records survive the correction.

### 1.2 Check the Railway environment for `SECRET_KEY` and `ADMIN_PASSWORD`

This decides the severity of the single Critical finding (§2.1). If both are set in Railway, C1 is
a latent trap rather than an active hole. If either is unset, the admin panel is currently
protected by a password committed to this repository.

### 1.3 GitHub authentication

`git fetch` fails with `Repository not found` + `Authentication failed` for
`github.com/Sakojadi/PoputkaKG.git`, and `gh` is not installed. **Nothing has been pushed.** All 22
commits exist only on this machine — do not delete the branch or reclone.

Once you can authenticate:

```bash
git push -u origin fix-oom-crash
```

GitHub prints a PR-creation link in the push output. (Optionally `brew install gh` for `gh pr create`.)

### 1.4 Deploy-time verification (after §2 is done)

1. Set `XPAY_CLIENT_ID`, `XPAY_CLIENT_SECRET`, `XPAY_MODE=sandbox` in Railway. Leave
   `PUBLIC_BASE_URL` unset so `RAILWAY_PUBLIC_DOMAIN` is used.
2. Confirm `POST https://<domain>/api/payment/xpay/webhook` returns `{"status":"ok"}` to an
   unauthenticated `curl`. If a proxy rule intercepts it, the callback silently never fires and you
   are on the button forever without knowing.
3. Pay a sandbox transaction at https://sandbox.xpay.kg **without** pressing "Проверить оплату" and
   confirm the bot advances on its own within a few seconds.
4. Before going live: confirm production credentials expose a service with `mode: PRODUCTION`.
   `_login()` raises if not — you want that failure at go-live, not at a user's first payment.

---

## 2. Fix before deploying — five items, all small

Every one is a few lines with essentially no regression risk.

### 2.1 `SECRET_KEY` / `ADMIN_PASSWORD` defaults — `bot/config.py:24-26` · CRITICAL

```python
admin_username: str = "admin"
admin_password: str = "admin12345"
secret_key: str = "secret-super-key-poputka-admin-xyz-123"
```

The signing key for admin session cookies is public in this repo. Anyone who reads it can forge an
admin session against any deployment that did not override it — and the admin panel can delete
campaigns, delete users, and ban people.

After confirming §1.2, delete the defaults so the next environment cannot silently inherit them.
Make `secret_key` and `admin_password` required (no default), so a missing value crashes at startup
instead of falling back to a known one.

**Nothing else on this list matters if this one is wrong.**

### 2.2 Session cookie is not `Secure` — `bot/web/app.py:39-43`

```python
app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key,
    max_age=3600 * 24 * 7,
    https_only=True,          # add this
)
```

One line. The admin session cookie is currently sent over plain HTTP if anything ever reaches the
app that way.

### 2.3 `_unwrap` raises `AttributeError` instead of `XPayError` — `bot/services/xpay.py:60-74`

`body = resp.json()` succeeds for *any* valid JSON — including a list or a bare string, which is
what proxies and CDNs return on an error page. The next line calls `body.get(...)`, which raises
`AttributeError`. That is not an `XPayError`, so it escapes every `except XPayError` handler,
including the one in `process_payment` — and the "Оплатить" button becomes a silent no-op with no
message to the user.

Add the guard next to the existing one for `data`:

```python
    body = resp.json()
    if not isinstance(body, dict):
        raise XPayError(f"xPay {path} returned a non-object body (HTTP {resp.status_code})")
```

### 2.4 Password change can lock you out permanently — `bot/web/app.py:1019-1023`

```python
await set_setting("admin_password_salt", salt.hex())
await set_setting("admin_password_hash", password_hash)
```

Two separate transactions. If the second fails, you have a **new salt with the old hash** — the old
password no longer verifies and the new one never will. You are locked out of your own panel with
no recovery path short of editing the database by hand.

Write both in one transaction.

### 2.5 Double-tap creates two campaigns for one payment — `bot/handlers/ad_flow.py:628-682`

`process_content_ok` creates a `Campaign` unconditionally. Tapping the confirm button twice
(easy on a laggy mobile connection) creates two campaigns for a single payment — the user gets
double the publications they paid for.

The `Payment` row already records the link. Guard on it: if `payment.campaign_id` is already set,
answer the callback and return instead of inserting a second campaign.

---

## 3. Do soon — not deploy-blocking, but real

**3.1 Image moderation can silently switch itself off — `bot/services/moderation.py:288-323`**
The OCR thread pool has 2 workers, and `asyncio.wait_for` cancels the *future*, not the thread. Two
hung tesseract runs occupy both workers forever; every later call then fails and returns
`True, ""` — **allowed** — through a bare `except Exception: pass`. Image moderation fails *open*,
process-wide, with no log line.

Ship the one-line partial fix immediately even before redesigning the pool:

```python
    except Exception as e:
        logger.warning(f"OCR moderation unavailable: {e}")
```

At least you will know it happened.

**3.2 The `payments` table is invisible to the admin panel — `bot/web/app.py`**
There is no `/admin/payments` view, and the delete routes orphan payment rows. Combined with the
in-memory FSM (a restart mid-flow strands a paying user), you currently have no UI to answer "did
this person actually pay?". A read-only page is the highest-*value* item on this list.

**3.3 `aiohttp.ClientSession()` per moderation call — `bot/services/moderation.py:347`**
Given this repo's OOM history, and the fact that `bot/services/tg.py` exists specifically because
per-call client construction leaked memory: hoist it to a module-level singleton. Do it before the
next traffic spike.

**3.4** Decide what "kick from group" means (`app.py:760-795` deletes the user row but does not ban
them, so they can rejoin) and add rate limits to the webhook and login form.

---

## 4. Smaller items

Listed in §"Follow-up" of the review. Each is a few lines and independent: scheduler mutated before
commit (M1), bot vs. panel revenue disagreeing (M2), bot token in a redirect URL (M3), raw exception
text in a redirect (M4), concurrent broadcasts (M5), a dead destructive route (M6), `init_db`
swallowing migration failures (M8), the webhook returning 200 on transient failures so xPay never
retries (M12), the check button dying with the FSM (M13), and a duplicate history entry on filter
restore (M16).

M9, M10 and M11 need no code change — they are awareness notes about single-process assumptions.

---

## 5. Deliberately not fixed

These were considered and rejected with reasons; don't let a future reviewer re-litigate them
without reading the rationale.

| Item | Why it stands |
| :--- | :--- |
| `datetime.utcnow` in model defaults (41 warnings) | All five models match, and `format_local` assumes naive timestamps. A partial migration is worse than the warning. Fix all five plus the renderer in one separate PR. |
| Float math in `amount_som * 100` | `price` is always `count × price_per_ad` from admin settings. Revisit only if fractional pricing appears. |
| `confirm_payment` living in `ad_flow.py` | It needs the `AdFlow` states; moving it creates a `services → handlers` import cycle. |
| `Unclosed client session` in tests | Pre-existing `get_bot()` teardown gap. A `close_bot()` fixture in `conftest.py` fixes it. |
| Two pre-existing `TRY401` lint findings | Predate all of this work; outside every file it touched. |

---

## 6. What is verified and trustworthy

Worth knowing which parts you do **not** need to re-examine:

- **No path reaches content entry, or creates a campaign, without a `COMPLETED` from
  `get_payment_status`.** Verified by enumerating all ten `set_state` calls.
- **The local-vs-deployed contract holds.** With no public URL configured, `callback_url` is
  genuinely never sent, and the flow stays correct through the button alone. Tests assert on the
  serialized request body, not on internal calls.
- **All 19 admin routes carry `Depends(require_admin)`.** The only unauthenticated route is the
  xPay webhook, which is deliberately public and never trusts its request body.
- **Live sandbox login, service-uuid resolution, and QR creation all succeed** against
  `devapi.xpay.kg`. The vendor's documented credentials still work.
- **The mock payment stub is entirely gone** — no surviving references anywhere.
