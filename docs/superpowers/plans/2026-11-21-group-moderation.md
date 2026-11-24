# Group Moderation + Private-Only Handlers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the bot answering in the group, moderate every group message with a mute→ban ladder (log-only by default), and remove `ADMIN_IDS`.

**Architecture:** All current handlers + `BanMiddleware` become private-chat-only. A new group-only router runs a pure classifier (`classify`) over each new/edited group message and a ledger-backed executor (`next_action` + Bot API calls). Mode (`off|log|enforce`), soft word list and event history are managed from a new `/admin/moderation` page.

**Tech Stack:** Python 3.12, aiogram ≥3.31, SQLAlchemy 2 async, FastAPI + Jinja2, pytest (`asyncio_mode = "auto"`), ruff (line length 100).

**Spec:** `docs/superpowers/specs/2026-11-21-group-moderation-design.md`

## Global Constraints

- Hard spam: 1st enforced offence → delete + **mute 24 h**; 2nd → delete + **ban with `revoke_messages=True`**. Hard strikes never expire.
- Soft: 1st → delete + warning; 2nd within a rolling **24 h** → delete + mute 24 h; 3rd within 24 h → delete + ban. Hard and soft counters are independent.
- Only **enforced** events count as strikes; log-only events are recorded with `enforced=False`.
- Default mode is **`log`**. Setting key `group_filter_mode`; soft list key `group_soft_words`; keyboard-removed flag key `group_keyboard_removed`.
- Exempt (never classified or recorded): the bot itself, any `is_bot` sender, `sender_chat` senders, group `creator`/`administrator` (checked with `get_chat_member` **only after a message is flagged**; clean messages make zero Bot API calls). If that check fails, take no action.
- Notices/warnings are deleted after **15 s** (`NOTICE_TTL_SECONDS = 15`).
- Words like «член», «соль», «трава» must not be flagged under any group rule.
- Only the group configured in `config.group_id` is moderated (`int` id, or `"@username"` string).
- A failing Bot API call (missing right, message already gone) is logged and never raised; remaining steps still run.
- No commits unless the owner asks. Baseline: `uv run pytest -q` → 60 pass, 1 pre-existing failure (`tests/test_payments_provider.py::test_mock_is_the_shipped_default`); ignore that one.

## Review Focus

- Photo/caption-only spam (no `text`): must be classified from `caption` (Task 4).
- Legitimate forwarded messages (no URL buttons, or only non-URL buttons): must not be flagged (Task 2).
- `GROUP_ID` configured as `"@username"`: group must still be matched (Task 4).
- A clean message edited into spam: handled like a new message (Task 4).
- Bot lacking rights / API errors on delete, restrict, ban: handler completes, event still recorded (Task 4).

---

### Task 1: Telegram test fakes, private-only handlers, drop `ADMIN_IDS`

**Files:**
- Create: `tests/tg_fakes.py`, `bot/dispatcher.py`, `tests/test_private_only.py`
- Modify: `tests/conftest.py`, `bot/handlers/__init__.py`, `bot/middlewares/ban.py`, `bot/main.py`, `bot/config.py`, `bot/handlers/ad_flow.py`
- Delete: `bot/handlers/admin.py`

**Interfaces:**
- Produces (`tests/tg_fakes.py`):
  - `GROUP_ID = -1001234567890`
  - `class FakeSession(BaseSession)`: `calls: list[tuple[str, dict]]` (method class name, `method.model_dump(exclude_none=True)`), `admin_ids: set[int]` (users `GetChatMember` reports as `administrator`, others `member`), `sent(name: str) -> list[dict]`. `SendMessage` returns a `Message` with an incrementing `message_id`; every other method returns `True`.
  - `make_bot() -> tuple[Bot, FakeSession]` (token `123456:TEST-TOKEN-NOT-REAL`)
  - `private_update(text: str, user_id: int = 777) -> Update`
  - `group_update(text: str | None = None, *, user_id: int = 777, message_id: int = 1, caption: str | None = None, forward: bool = False, buttons: Sequence[tuple[str, str | None]] = (), is_bot: bool = False, sender_chat_id: int | None = None, edited: bool = False, chat_id: int = GROUP_ID) -> Update` — `buttons` become an inline keyboard (`(label, url)`; `url=None` → callback button); `forward=True` sets `forward_origin`; `edited=True` puts the message in `Update.edited_message`.
- Produces (`bot/dispatcher.py`): `build_dispatcher() -> Dispatcher` — creates the `Dispatcher`, registers `BanMiddleware` on `message` and `callback_query` outer middleware, includes `get_handlers_router()`. `main.py` calls it instead of doing this inline. Routers are module-level singletons, so it can be called only once per process.
- Produces (`tests/conftest.py`): session-scoped sync fixture `dispatcher` (`build_dispatcher()`); function fixture `tg(db, dispatcher)` yielding a `SimpleNamespace(bot, session, feed)` where `feed = async (update) -> None` calls `dispatcher.feed_update(bot, update)`; after each test clear the dispatcher's `MemoryStorage` so FSM state doesn't leak. Fixture `group_chat` sets `bot.config.config.group_id = GROUP_ID` and restores it.

- [ ] **Step 1: Write failing tests** in `tests/test_private_only.py` (using `tg`): 
  - `test_ad_button_in_group_gets_no_reply`: `group_update(TEXTS["ky"]["ad_button"])` → `tg.session.sent("SendMessage") == []`.
  - `test_start_command_in_group_gets_no_reply`: same with `"/start"`.
  - `test_group_message_creates_no_user_row`: after a group message, `select(func.count()).select_from(User)` == 0.
  - `test_banned_user_gets_no_reply_in_group`: seed `User(id=777, is_banned=True)`, group message → no `SendMessage`.
  - `test_private_start_still_answers`: `private_update("/start")` → exactly one `SendMessage` with `chat_id == 777`.
  - `test_admin_ids_setting_removed`: `"admin_ids" not in Settings.model_fields`, and `bot.handlers` has no `admin` module (`importlib.util.find_spec("bot.handlers.admin") is None`).
- [ ] **Step 2: Run** `uv run pytest tests/test_private_only.py -v` — expected: FAIL (fakes/dispatcher missing).
- [ ] **Step 3: Implement** `tests/tg_fakes.py`, `bot/dispatcher.py` and the fixtures per the Interfaces block; switch `bot/main.py` to `build_dispatcher()`.
- [ ] **Step 4: Make handlers private-only.** In `bot/handlers/__init__.py` wrap `start_router` and `ad_flow_router` in one parent `Router` whose `message` observer has filter `F.chat.type == "private"` and whose `callback_query` observer has `F.message.chat.type == "private"`. In `BanMiddleware.__call__`, when `data.get("event_chat")` exists and its `type != "private"`, `return await handler(event, data)` before any DB access.
- [ ] **Step 5: Remove `ADMIN_IDS`.** Delete `bot/handlers/admin.py` and its import/registration in `bot/handlers/__init__.py`; delete the `admin_ids` field and `parse_admin_ids` validator in `bot/config.py`; delete `_notify_admins_stranded_payment` in `bot/handlers/ad_flow.py` (nothing calls it — confirm with grep).
- [ ] **Step 6: Run** `uv run pytest -q` — expected: all pass except the one pre-existing failure.

### Task 2: Classifier and soft word list

**Files:**
- Create: `bot/services/group_filter.py`, `tests/test_group_filter.py`
- Modify: `bot/services/moderation.py` (extract link check)

**Interfaces:**
- Produces (`moderation.py`): `find_disallowed_link(text: str) -> str | None` — the URL loop currently inside `moderate_text`, returning the first non-allowed URL; `moderate_text` calls it (behaviour unchanged, existing tests must still pass).
- Produces (`group_filter.py`):
  - `@dataclass(frozen=True) class Verdict: kind: str  # "hard"|"soft"; rule: str  # "forwarded_link_buttons"|"adult_phrase"|"soft_word"|"disallowed_link"; matched: str`
  - `classify(text: str | None, *, forwarded: bool = False, buttons: Sequence[tuple[str, str | None]] = ()) -> Verdict | None` — `buttons` are `(label, url)`. Order: (1) hard `forwarded_link_buttons`: `forwarded` and any button has a url; (2) hard `adult_phrase`: `HARD_PHRASES` over `text` + all button labels; (3) soft `soft_word` over the same combined text; (4) soft `disallowed_link` via `find_disallowed_link(text)`.
  - `HARD_PHRASES: list[str]` (regex, case-insensitive): at minimum `18\s*\+\s*only`, `hot\s+girls?`, `squirt`, `live\s*cam`, `\bpov\s+sex`, `\bbj\s+vid`, `watch\s+(all\s+)?videos?\s+free`, `full\s+video`, `no\s+blur`, `onlyfans`, `\bporn`, `порно`, `эскорт`, `проститутк`, `секс\s*видео`.
  - `DEFAULT_SOFT_WORDS: list[str]` — hand-picked from `moderation.DEFAULT_BANNED_WORDS`: `\b`-anchored profanity, explicit drugs/gambling stems; **exclude** short/ambiguous entries (`член`, `соль`, `соли`, `трав`, `меф`, `вирт`, `куни`, `трах`, `ам\b`, `mef`, `soli`, `mraz`, `hui`, `hye`, `blad`, `eban`).
  - `SOFT_WORDS_KEY = "group_soft_words"`; `get_soft_words() -> list[str]`; `async reload_soft_words() -> None`; `async save_soft_words(words: list[str]) -> None` — same persistence pattern as `moderation.save_banned_words` (JSON in `app_settings`, invalid regex skipped with a warning, invalid stored JSON → defaults).

- [ ] **Step 1: Write failing tests** in `tests/test_group_filter.py` (pure unless noted):
  - hard: `classify("FREE HOT GIRL VIDEOS Full video · No blur · Watch now 18+ only — tap button")` → `Verdict("hard", "adult_phrase", …)`.
  - hard via labels: `classify("Watch", buttons=[("POV SEX", None), ("LIVE CAM", None)])` → `adult_phrase`.
  - hard structural: `classify("Watch", forwarded=True, buttons=[("Open", "https://x.example")])` → `forwarded_link_buttons`.
  - **not flagged:** forwarded + no buttons; forwarded + `[("Reply", None)]`; `None`/`""` text.
  - **not flagged (words):** `"Нужен член семьи на заднее сиденье"`, `"соль не везём"`, `"трава на обочине"`, `"Бишкек Ош жеңил машина 0555123456"`, `"Звоните wa.me/996555123456"`.
  - soft: `"ты сука"` → `Verdict("soft", "soft_word", …)`; `"смотри instagram.com/foo"` → `disallowed_link`.
  - `async` with `db`: `save_soft_words(["\\bплохо"])` then `reload_soft_words()` → `classify("это плохо").rule == "soft_word"`; an invalid pattern in the list is skipped, not fatal; autouse fixture restores defaults.
- [ ] **Step 2: Run** `uv run pytest tests/test_group_filter.py -v` — expected: FAIL (module missing).
- [ ] **Step 3: Implement** `find_disallowed_link` refactor and `group_filter.py` per Interfaces.
- [ ] **Step 4: Run** `uv run pytest tests/test_group_filter.py tests/test_banned_words.py -v` — expected: PASS.

### Task 3: Strike ledger, escalation ladder, mode setting

**Files:**
- Create: `bot/services/group_enforcement.py`, `tests/test_group_enforcement.py`
- Modify: `bot/database/models.py`

**Interfaces:**
- Produces (`models.py`): `class ModerationEvent(Base)`, table `moderation_events`: `id` int pk autoincrement, `user_id` BigInteger (indexed), `chat_id` BigInteger, `message_id` BigInteger, `kind` String, `rule` String, `action` String, `enforced` Boolean, `snippet` String, `created_at` DateTime default `datetime.utcnow` (naive UTC, like other models). No migration needed (`create_all`).
- Produces (`group_enforcement.py`):
  - `class Action(StrEnum): WARN = "warn"; MUTE = "mute"; BAN = "ban"`
  - `MUTE_HOURS = 24`; `SOFT_WINDOW = timedelta(hours=24)`
  - `async next_action(user_id: int, kind: str, now: datetime | None = None) -> Action` — counts only `enforced=True` events. hard: 0 prior → `MUTE`, ≥1 → `BAN` (no expiry). soft: prior within `SOFT_WINDOW` 0 → `WARN`, 1 → `MUTE`, ≥2 → `BAN`.
  - `async record_event(*, user_id: int, chat_id: int, message_id: int, kind: str, rule: str, action: Action, enforced: bool, snippet: str) -> None` (snippet truncated to 200 chars)
  - `async recent_events(limit: int = 100) -> list[ModerationEvent]` newest first
  - `MODES = ("off", "log", "enforce")`; `async get_mode() -> str` (default and any invalid stored value → `"log"`); `async set_mode(mode: str) -> None` (raises `ValueError` outside `MODES`)

- [ ] **Step 1: Write failing tests** (with `db`): hard none→`MUTE`; one enforced hard → `BAN`; one hard with `enforced=False` → still `MUTE`; hard strike 30 days old still → `BAN`; soft none→`WARN`, one enforced soft <24 h → `MUTE`, two → `BAN`; one soft 25 h old → `WARN`; one hard strike does not change soft result (`WARN`); `get_mode()` default `"log"`; stored `"bogus"` → `"log"`; `set_mode("enforce")` round-trips; `set_mode("x")` raises `ValueError`; `recent_events` order and snippet truncation.
- [ ] **Step 2: Run** `uv run pytest tests/test_group_enforcement.py -v` — expected: FAIL.
- [ ] **Step 3: Implement** model + service per Interfaces (SQLAlchemy `select` with `func.count`; mode via `settings_store.get_setting` / `set_setting`).
- [ ] **Step 4: Run** the same command — expected: PASS.

### Task 4: Group router, executor, keyboard cleanup, startup wiring

**Files:**
- Create: `bot/handlers/group_moderation.py`, `tests/test_group_moderation.py`
- Modify: `bot/handlers/__init__.py`, `bot/main.py`

**Interfaces:**
- Consumes: `classify`, `Verdict` (Task 2); `next_action`, `record_event`, `get_mode`, `Action`, `MUTE_HOURS` (Task 3); `ad_flow._spawn`; fixtures/fakes (Task 1).
- Produces (`group_moderation.py`):
  - `router` handling `message` and `edited_message` with the same coroutine `moderate_group_message(message: Message, bot: Bot) -> None`, gated to group/supergroup chats whose chat is the configured group (`is_target_group(chat: Chat) -> bool`: `int` group_id compares `chat.id`; `"@name"` compares case-insensitively with `"@" + chat.username`).
  - `NOTICE_TTL_SECONDS = 15`
  - `async ensure_group_keyboard_removed(bot: Bot) -> None` — if setting `group_keyboard_removed` is unset and `config.group_id` is set: send the group a bilingual (ky/ru) "the bot now works only in private messages" message with `ReplyKeyboardRemove()`, delete it after `NOTICE_TTL_SECONDS`, and set the flag **only if the send succeeded**.
- Handler flow: skip exempt senders (bot, `is_bot`, `sender_chat`) → `get_mode()`; `off` → return → build facts (`text or caption`, buttons from `reply_markup.inline_keyboard`, `forwarded = forward_origin is not None`) → `classify`; `None` → return (no Bot API calls) → `get_chat_member`; creator/administrator or failure → return → `action = await next_action(...)` → `log` mode: `record_event(enforced=False)` only → `enforce`: delete message, then `WARN`: bilingual warning naming the user via `mention_html`; `MUTE`: `restrict_chat_member` with all send permissions `False` and `until_date = now + 24 h`, plus notice; `BAN`: `ban_chat_member(..., revoke_messages=True)` plus notice; notices deleted after `NOTICE_TTL_SECONDS` via `_spawn`; finally `record_event(enforced=True)`.
- Wiring: include the group router after the private router in `get_handlers_router()`; add `"edited_message"` to `allowed_updates` in `main.py`; call `await ensure_group_keyboard_removed(bot)` in `main()` after `get_bot()`.

- [ ] **Step 1: Write failing tests** (`tg`, `group_chat`; mode via `set_mode`; spam text = the screenshot string from Task 2; set `NOTICE_TTL_SECONDS = 0` where cleanup is asserted and `await asyncio.gather(*ad_flow._background_tasks)`):
  - `enforce`, hard first → calls include `DeleteMessage(chat_id=GROUP_ID, message_id=…)`, `RestrictChatMember(user_id=777, until_date ≈ now+24 h)` and a `SendMessage`; one `ModerationEvent(enforced=True, action="mute", kind="hard")`; the notice is deleted afterwards.
  - hard second → `BanChatMember` with `revoke_messages=True`.
  - soft ladder: 1st → delete + warning, no `RestrictChatMember`; 2nd → restrict; 3rd → ban.
  - `log` mode: only `GetChatMember` is called (no Delete/Restrict/Ban/Send); one event `enforced=False`; a following `enforce` message is still a first offence.
  - `off` mode: `session.calls == []`, no events.
  - clean message → `session.calls == []` (no `GetChatMember`).
  - exempt: `session.admin_ids = {777}` + spam → no Delete/Restrict/Ban, no event; `is_bot=True` sender, `sender_chat_id=-100555`, and `user_id == tg.bot.id` → ignored.
  - **caption-only** spam (`text=None, caption=…`) → deleted.
  - **edited** spam (`edited=True`) → deleted.
  - other chat (`chat_id=-100999`) → `session.calls == []`.
  - `GROUP_ID="@taxigroup"` with a group chat whose `username="taxigroup"` → moderated (extend `group_update` with `chat_username` if needed).
  - API errors: make `DeleteMessage` raise `TelegramBadRequest` → handler does not raise, restrict still sent, event recorded; `GetChatMember` raising → no action, no event.
  - `ensure_group_keyboard_removed`: called twice → exactly one `SendMessage` containing a `ReplyKeyboardRemove`; send failure → flag not set (second call retries).
- [ ] **Step 2: Run** `uv run pytest tests/test_group_moderation.py -v` — expected: FAIL.
- [ ] **Step 3: Implement** `group_moderation.py` and wiring per Interfaces; the warning/notice text is bilingual ky+ru in one string per action (constants in the module).
- [ ] **Step 4: Run** `uv run pytest tests/test_group_moderation.py tests/test_private_only.py -v` — expected: PASS.
- [ ] **Step 5: Smoke** — `uv run python -c "import bot.main"` with test env vars (fake `BOT_TOKEN`, scratch `DATABASE_URL`) imports without error.

### Task 5: Admin panel page

**Files:**
- Create: `bot/web/templates/moderation.html`, `tests/test_moderation_admin.py`
- Modify: `bot/web/app.py`, `bot/web/templates/settings.html` (one link card to the new page)

**Interfaces:**
- Consumes: `get_mode`, `set_mode`, `MODES`, `recent_events` (Task 3); `get_soft_words`, `save_soft_words`, `DEFAULT_SOFT_WORDS` (Task 2).
- Produces routes (all `Depends(require_admin)`, patterned on the existing banned-words routes, redirecting back with `msg`/`err`): `GET /admin/moderation` (template `moderation.html`, `active_page="settings"`; context: `mode`, `modes`, `soft_words`, `events`), `POST /admin/moderation/mode` (`mode: str = Form(...)`; invalid → `err`), `POST /admin/moderation/words/add` (validates with `re.compile`, rejects duplicates), `POST /admin/moderation/words/delete`, `POST /admin/moderation/words/reset` (→ `DEFAULT_SOFT_WORDS`). The page shows the mode switch with a one-line explanation of each mode (stating log mode takes no action), the soft-word editor (mirror `settings.html`'s banned-words card), and a table of the last 100 events (time via the `localdt` filter, user id, kind, rule, action, `enforced` badge, snippet).

- [ ] **Step 1: Write failing tests** calling the route coroutines directly with `_=True` (as `tests/test_webhook.py` does): `mode_action("enforce")` persists and redirects; `mode_action("bogus")` leaves mode unchanged and redirects with `err`; add valid word persists; add invalid regex `"(unclosed"` rejected; duplicate rejected; delete removes; reset restores defaults. One render test: `moderation_page` called with a real `starlette.requests.Request` scope returns 200 and contains a seeded event's snippet.
- [ ] **Step 2: Run** `uv run pytest tests/test_moderation_admin.py -v` — expected: FAIL.
- [ ] **Step 3: Implement** routes and template; add a card in `settings.html` linking to `/admin/moderation`.
- [ ] **Step 4: Run** the test file — expected: PASS.

### Task 6: Docs and final verification

**Files:**
- Modify: `README.md` (if it mentions admin command/`ADMIN_IDS`; otherwise add a short "Group moderation" section: modes, rules, required bot rights **Delete messages** and **Ban users**, `ADMIN_IDS` no longer used)
- Modify: `docs/superpowers/specs/2026-11-21-group-moderation-design.md` (web-panel line: new `/admin/moderation` page linked from Settings instead of "on the Settings page")

- [ ] **Step 1:** Update the docs above; `grep -rniI "admin_ids" bot tests README.md docs/superpowers` returns nothing.
- [ ] **Step 2:** `uv run ruff check bot tests` — expected: no new findings in touched files.
- [ ] **Step 3:** `uv run pytest -q` — expected: all new tests pass; only the one pre-existing failure remains.
- [ ] **Step 4:** Re-run the scratch repro (`.../scratchpad/repro_group.py`) — expected: bot sends nothing for any of the four group messages.
