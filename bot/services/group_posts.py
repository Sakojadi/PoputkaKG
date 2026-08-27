import json
import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import config
from bot.database.db import AsyncSessionLocal
from bot.database.models import GroupPost
from bot.services.tg import get_bot

logger = logging.getLogger(__name__)

# Mirrors ad_flow.py's campaign job: deactivate a post whose sends keep failing
# instead of retrying forever.
MAX_CONSECUTIVE_FAILURES = 5

# Mirrors ad_flow.py's campaign job: cap the stored history so the column
# cannot grow without bound.
MAX_TRACKED_MESSAGE_IDS = 2000


def build_group_post_markup(buttons_json: str) -> InlineKeyboardMarkup | None:
    try:
        rows = json.loads(buttons_json or "[]")
    except (TypeError, ValueError):
        return None
    if not rows:
        return None
    buttons = []
    for row in rows:
        text, url = row.get("text"), row.get("url")
        if text and url:
            buttons.append(InlineKeyboardButton(text=text, url=url))
    if not buttons:
        return None
    # One button per row: cramming every button into a single row (the
    # previous behavior) makes them unreadably narrow past 2-3 buttons.
    return InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons])


async def post_group_message(post_id: int):
    from bot.services.scheduler import remove_group_post_job

    async with AsyncSessionLocal() as session:
        post = await session.get(GroupPost, post_id)
        if not post or not post.is_active or post.repeats_left <= 0:
            return

        if not config.group_id:
            # A missing GROUP_ID is a deployment problem, not a problem with
            # this post: abort without touching the counter and let the job
            # retry once the env var is restored.
            logger.error(
                f"Skipping group post #{post_id}: GROUP_ID is not configured"
            )
            return

        bot = get_bot()
        try:
            markup = build_group_post_markup(post.buttons)
            sent_msg = await bot.send_message(
                chat_id=config.group_id, text=post.text or ".", reply_markup=markup
            )

            if sent_msg and hasattr(sent_msg, "message_id"):
                post.last_message_id = sent_msg.message_id
                existing = [
                    x.strip() for x in (post.message_ids or "").split(",") if x.strip()
                ]
                existing.append(str(sent_msg.message_id))
                post.message_ids = ",".join(existing[-MAX_TRACKED_MESSAGE_IDS:])

            logger.info(f"Successfully posted group post #{post_id} to {config.group_id}")

            post.repeats_left -= 1
            post.failure_count = 0

            if post.repeats_left <= 0:
                post.is_active = False
                remove_group_post_job(post)

            await session.commit()

        except Exception as e:
            try:
                await session.rollback()

                # rollback() expires every instance in the session, so touching
                # the old `post` object would trigger a lazy refresh SELECT and
                # raise MissingGreenlet under AsyncSession. Re-fetch instead.
                post = await session.get(GroupPost, post_id)
                if post is None:
                    return

                post.failure_count = (post.failure_count or 0) + 1
                logger.error(
                    f"Failed to send group post #{post_id} to group {config.group_id} "
                    f"(failure {post.failure_count}/{MAX_CONSECUTIVE_FAILURES}): {e}."
                )

                if post.failure_count >= MAX_CONSECUTIVE_FAILURES:
                    post.is_active = False
                    remove_group_post_job(post)
                    logger.error(
                        f"Group post #{post_id} deactivated after "
                        f"{post.failure_count} consecutive failures."
                    )

                await session.commit()
            except Exception:
                # The original post failure is already logged above.
                logger.exception(
                    f"Could not persist failure state for group post #{post_id}"
                )
            return
