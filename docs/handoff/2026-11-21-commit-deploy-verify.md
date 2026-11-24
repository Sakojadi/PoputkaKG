# Prompt for the next session: commit, confirm the deploy, verify everything works

> Paste this whole file as the first message of a new Claude Code session in
> `/Users/zza03/Documents/GitHub/PoputkaKG`. It is written to the future assistant.

You are picking up finished, **uncommitted** work. Your job: (1) commit it, (2) get it
deployed and confirm the deploy is done, (3) verify the live system works. The owner
wrote this prompt knowing it authorizes steps 1–3 below, with the safety rules at the end.

## State of the work (as of 2026-11-21)

- Branch: `group-moderation` (forked from `main` at `bd5f041`). **Nothing is committed.**
  Check with `git status`; new files were marked intent-to-add (`git add -N`), nothing is staged for real.
- Production: Railway (`railway.toml`: start `uv run python -m bot.main`, healthcheck `/health`,
  `restartPolicyType=always`). GitHub remote: `origin` = `Sakojadi/PoputkaKG`. The production DB is
  **Postgres** (asyncpg); tests run on SQLite, so Postgres behaviour is **not yet verified**.
- Spec: `docs/superpowers/specs/2026-11-21-group-moderation-design.md`
  Plan: `docs/superpowers/plans/2026-11-21-group-moderation.md`

What changed:
1. **Bug fix:** the bot answered in the 72k-member group (no chat-type filter; a `/start` in the group
   posted the welcome + reply keyboard there, and members then ran the ad flow in the group).
   All handlers and `BanMiddleware` are now private-chat-only (`bot/handlers/__init__.py`,
   `bot/middlewares/ban.py`); `bot/dispatcher.py` builds the dispatcher; on startup the bot sends the
   group one self-deleting notice with `ReplyKeyboardRemove` (flag `group_keyboard_removed` in `app_settings`).
2. **Group moderation** (`bot/handlers/group_moderation.py`, `bot/services/group_filter.py`,
   `bot/services/group_enforcement.py`, new table `moderation_events`, page `/admin/moderation`).
   Spam: 1st offence delete + mute 24 h, 2nd ban (revoke messages). Rule violations: warning, then mute
   (2nd within 24 h), then ban (3rd within 24 h). Group admins/bots/channel senders are exempt. Edited
   messages are checked (`edited_message` added to `allowed_updates`).
   **Default mode is `log`** (records what it would do, takes no action).
3. **`ADMIN_IDS` removed** (config field, `/admin` bot command, dead notifier). The env var is now ignored.

Tests: `uv run pytest -q` → 175 pass + 1 **pre-existing** failure
(`tests/test_payments_provider.py::test_mock_is_the_shipped_default`, fails on untouched `main`; mention it, don't "fix" it
as part of this).

## Step 1 — Commit

1. `uv run pytest -q` and `uv run ruff check bot tests` first. Report failures by name.
2. **Do not commit:** `.DS_Store` files, `.superpowers/` (ledger/scratch — it is *not* gitignored here;
   ask the owner whether to add it to `.gitignore`), anything under the session scratchpad, `.env`, `*.db`.
3. Stage explicitly by path (no `git add -A`). Suggested split: one commit for `docs/superpowers/**`
   and this handoff only if the owner wants it tracked, one commit for code + tests + `README.md`. Files overlap
   (`bot/main.py`, `bot/handlers/__init__.py`, `tests/conftest.py`), so don't try to split the code by feature.
4. Commit message: explain the *why* (bot replied in the group; spam in the group; ADMIN_IDS unused) and end with
   the `Co-Authored-By:` line from the session's attribution reminder.

## Step 2 — Get it deployed and confirm it finished

1. Load the Railway tools (`ToolSearch` → `select:mcp__railway__whoami,mcp__railway__list-projects,mcp__railway__list-services,mcp__railway__list-deployments,mcp__railway__get-status,mcp__railway__get-logs,mcp__railway__get-deployment-diagnosis,mcp__railway__list-variables`)
   and use the `use-railway` skill. Find the project/service for this bot.
2. Find out **which branch/commit Railway deploys from** (service source settings, or `describe-service`). Do not assume `main`.
3. If the deploy source is `main`: the work must be merged into `main` and pushed (`git push origin main`). That is
   outward-facing and triggers a production deploy, so **confirm with the owner unless their message already said to
   push/merge**. Never force-push. If `git push` fails on authentication (an earlier session saw
   `Repository not found` / auth failure and `gh` not installed — see `docs/NEXT_STEPS.md` §1.3), stop and tell the owner;
   do not work around it.
4. Watch the new deployment until it is `SUCCESS` and the healthcheck passes: `list-deployments`, then `get-logs`.
   If it fails, use `get-deployment-diagnosis` and the `systematic-debugging` skill; don't guess.
5. Confirm the deployed commit SHA equals the one you committed.

## Step 3 — Verify it works (in order; stop and report at the first real failure)

**Startup logs** (look for each, report any traceback/ERROR):
- `Database dialect: postgresql`, `Starting Bot & Web Admin Panel`, no errors from `init_db` (the `moderation_events`
  table is created by `create_all`; confirm there is no SQL error — this is the first run on Postgres).
- Either the keyboard-removal notice was sent once (no `Could not remove the group reply keyboard` warning) or it warns —
  if it warns, the bot likely lacks rights in the group; tell the owner. Don't loop on it.
- No polling errors; `allowed_updates` includes `edited_message`.

**Live checks** (read-only, no posting in the real group without the owner's say-so):
- `GET <public url>/health` returns 200.
- Admin panel: log in, open `/admin/moderation` → renders, mode shows `log`, word list and (initially empty) events table
  show. (Credentials are in the owner's Railway variables / `.env`; never print them.)
- Railway variables: `GROUP_ID` is set. `ADMIN_IDS` can be deleted (now ignored) — suggest it, don't do it unasked.
- Ask the owner to confirm in Telegram, from a **non-admin** account: `/start` in the group gets **no** bot reply; `/start`
  in the private chat still answers; a test message in the group containing a hard-spam phrase shows up on
  `/admin/moderation` as an event with `только журнал` (log-only) and is **not** deleted. Check the bot's rights in the
  group: Delete messages + Ban users.
- Watch logs for ~10 minutes of real traffic: no exceptions from `group_moderation`, no `Group moderation: ... failed`
  floods, and note the rate of events. CPU/memory in Railway metrics (`get-service-metrics`) should look unchanged
  (expected cost: well under 1% of a core).

**Then report**: commit SHA(s), deployment id + status, what you verified, what you could not verify (Telegram-side
behaviour needs the owner), and the count/examples of log-only events so the owner can judge false positives.

## Safety rules

- **Do not switch the mode to `enforce`.** That is the owner's decision after reviewing the log for a few days.
  Don't change it through the panel or the database.
- No `git push --force`, no `reset --hard`, no deleting branches, no skipping hooks.
- Don't read `.env` into the conversation or send project details/credentials to any external service.
- Report faithfully: if something is red or unverifiable, say so plainly; don't claim "working" from a green deploy alone.

## Known, deliberately deferred (don't "fix" unasked)

- Hard phrases `full video` / `hot girls` / `no blur` are generic English (a dashcam "full video" post would count as spam).
- Soft defaults `закладк` / `казино` match ordinary words (warning only).
- The mute/ban notice repeats the spammer's display name for 15 s.
- In log mode each edit of a flagged message adds another event row.
- An already-saved word list overrides the defaults: new default words only apply after "Сбросить к умолчанию"
  on `/admin/moderation` (or on a list that was never edited).
- Spam with no link/button/banned word (e.g. "Majaharul iltimos ssilka tashamang!") is not caught by any rule.
