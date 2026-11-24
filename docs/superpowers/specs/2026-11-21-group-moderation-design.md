# Group moderation + private-chat-only handlers — design

Date: 2026-11-21 · Size: medium (three coupled changes, one PR) · Status: approved by owner in conversation

## Intent

The bot is an admin in a 72k-member taxi group. Two problems were reported:

1. **The bot talks in the group.** No handler checks the chat type, so a `/start` typed in the
   group made the bot post the welcome message + `main_keyboard` there (a reply keyboard visible
   to *everyone*). Members then tapped "📢 Жарнама берүү" in the group, started the ad flow
   there, and got "⚠️ Сураныч, туура сан жазыңыз" replies. Reproduced with the real dispatcher.
   `BanMiddleware` has the same flaw (answers "account blocked" in the group, inserts a `User`
   row for every group member who posts).
2. **Spam in the group** (porn-bot forwards with link buttons, scam bots). Admins delete it by
   hand. The owner wants the bot to filter every user's messages automatically.

## Goals

- Every existing handler and `BanMiddleware` act only in private chats.
- Remove the stale reply keyboard from the group once.
- Moderate every non-exempt group message (new **and edited**) and escalate repeat offenders.
- Ship in **log-only** mode first so false positives can be reviewed before anything is enforced.
- Remove `ADMIN_IDS` entirely (config field, `/admin` bot command, dead
  `_notify_admins_stranded_payment`).

## Non-goals / anti-goals

- No photo OCR / image moderation for group messages (the only expensive check; later if needed).
- Must never ban or mute a legitimate driver because of an ordinary word ("член семьи", "соль",
  "трава" are bannable under **no** rule — they stay out of the hard set and out of the group
  list's defaults).
- No account-age signal (Bot API does not expose it).
- No new admin UI beyond: mode switch, group word list editor, read-only events page.

## Rules (agreed with owner)

| Signal | 1st offence | 2nd offence | later |
|---|---|---|---|
| **Hard spam**: forwarded message with link buttons; 18+/porn phrases; "18+ / watch free"-style text | delete + **mute 24 h** | delete + **ban** (revoke their messages) | — |
| **Soft**: profanity, drugs, gambling, disallowed link (group word list) | delete + 15 s warning | within 24 h: delete + **mute 24 h** | 3rd within 24 h: **ban** |
| Clean | nothing | | |

My calls (not stated by the owner — flag in review):
- Hard strikes never expire (a spammer returning after the 24 h mute is still on strike 2).
  Soft strikes count within a rolling 24 h. The two counters are independent; ban when
  `hard >= 2` or `soft >= 3`.
- Exempt: the bot itself, anonymous/channel senders (`sender_chat`), and Telegram group
  administrators/creator. Admin status is checked with `get_chat_member` **only for flagged
  messages**, so clean traffic pays nothing. `ADMIN_IDS` is not involved.
- Inspected text: `text`, `caption`, inline-keyboard button labels + URLs, URL entities, and the
  forward flag. (Screenshot spam carried its giveaway words only in button labels.)
- Hard-spam rules live in code (structural signals + a small fixed phrase list); the **soft**
  group word list is stored in `app_settings` and editable in the web panel like the existing
  ad banned-words list, but is a **separate list** with its own conservative defaults.
- Warning/notice messages are deleted after ~15 s by a background task (lost on restart —
  acceptable).
- Only **enforced** actions count as strikes. Log-only mode records the would-be first-rung
  action but does not create strikes, so enabling enforcement never retroactively bans anyone.

## Approaches considered

1. **(chosen) New group-only router + pure classifier module.** A handler for `message` and
   `edited_message` in `group`/`supergroup` chats calls
   `classify(message) -> Verdict` (no Telegram I/O, unit-testable with the screenshot spam), then
   an executor applies the action (delete / restrict / ban / warn) and records an event.
2. Outer middleware doing the check — rejected: runs for every update type and entangles group
   moderation with private-chat logic being separated by fix 1.
3. Separate worker process — rejected: Telegram allows one polling consumer per token, and it
   would duplicate settings/DB access.

## Parts

- `bot/handlers/*`, `BanMiddleware`: private-chat filter (`F.chat.type == "private"`).
- `bot/main.py`: add `edited_message` to `allowed_updates`; register group router; on startup
  (or first group message) send the group a one-time `ReplyKeyboardRemove` notice, then delete it.
- `bot/services/group_filter.py`: classifier (pure) + hard/soft rules + soft list load/save.
- `bot/handlers/group_moderation.py`: router, exemptions, executor, warning cleanup.
- DB: `moderation_events` (id, user_id, chat_id, message_id, kind `hard|soft`, rule, action,
  enforced bool, snippet, created_at) — doubles as the strike ledger. Created by
  `create_all`; no migration needed for a new table.
- Settings: `group_filter_mode` = `off | log | enforce`, **default `log`**.
- Web panel: new `/admin/moderation` page (linked from Settings) with the mode switch, the
  soft-word list editor and a read-only list of the last 100 events.
- Remove `admin_ids` from `config.py`, delete `handlers/admin.py` and its router registration,
  delete `_notify_admins_stranded_payment`; update docs mentioning them. (`ADMIN_IDS` in
  `.env`/Railway becomes ignored — `extra="ignore"` already covers it.)

## Constraints

- The bot needs **Delete messages** and **Ban users** admin rights in the group. A failure to
  act (missing right, message already deleted) is logged and recorded, never raised.
- CPU: regex pass measured ~0.2–0.65 ms per message; negligible.
- Existing tests keep passing; new tests cover classifier, ladder, exemptions, private-only
  filtering and mode behaviour using a fake Bot session.

## Left to the builder

Exact hard-phrase list and soft defaults (start conservative), events-page layout, warning
wording in ky/ru, and how the one-time keyboard removal is triggered.
