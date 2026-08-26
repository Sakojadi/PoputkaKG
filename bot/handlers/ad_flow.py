import asyncio
import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import config
from bot.database.db import AsyncSessionLocal
from bot.database.models import Campaign, Payment, User
from bot.handlers.start import get_user_lang, main_keyboard
from bot.locales.translations import TEXTS, get_text
from bot.services.fsm import get_fsm_context
from bot.services.moderation import moderate_full_content
from bot.services.scheduler import remove_campaign_job, schedule_campaign
from bot.services.settings_store import get_price_per_ad
from bot.services.tg import get_bot
from bot.services.xpay import XPayError, create_payment, get_payment_status

logger = logging.getLogger(__name__)
router = Router()

# A campaign whose posts keep failing (bot kicked from the group, expired photo,
# deleted chat) is deactivated instead of retrying forever.
MAX_CONSECUTIVE_FAILURES = 5

# Cap the stored history so the column cannot grow without bound. Must stay well
# above any realistic publications_total: admin "delete from group" can only
# remove the posts listed here, so a truncated history leaves posts undeletable.
MAX_TRACKED_MESSAGE_IDS = 2000

# asyncio only holds a weak reference to running tasks, so a fire-and-forget
# task can be garbage collected mid-execution unless we keep it alive.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


class AdFlow(StatesGroup):
    count = State()
    interval = State()
    confirm_payment = State()
    waiting_payment = State()
    content = State()
    confirm_content = State()


async def post_ad(user_id: int, campaign_id: int):
    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        if not campaign or not campaign.is_active or campaign.publications_left <= 0:
            return

        # Check if the user is banned in bot
        user = await session.get(User, user_id)
        if user and getattr(user, "is_banned", False):
            logger.warning(
                f"Aborting ad post for campaign {campaign_id}: User {user_id} is banned."
            )
            campaign.is_active = False
            remove_campaign_job(campaign)
            await session.commit()
            return

        lang = await get_user_lang(user_id, session)
        total_published = campaign.publications_total

        if not config.group_id:
            # A missing GROUP_ID is a deployment problem, not a problem with this
            # campaign: counting it as a failure would deactivate every active
            # campaign at once, with publications left and no refund. Abort
            # without touching the counter and let the job retry once the env var
            # is restored -- retrying is cheap now that the Bot is a singleton.
            logger.error(
                f"Skipping ad post for campaign #{campaign_id}: "
                "GROUP_ID is not configured in settings/env"
            )
            return

        bot = get_bot()
        try:
            if campaign.content_photo:
                sent_msg = await bot.send_photo(
                    chat_id=config.group_id,
                    photo=campaign.content_photo,
                    caption=campaign.content_text or "",
                )
            else:
                sent_msg = await bot.send_message(
                    chat_id=config.group_id, text=campaign.content_text or "."
                )

            if sent_msg and hasattr(sent_msg, "message_id"):
                campaign.last_message_id = sent_msg.message_id
                existing = [
                    x.strip()
                    for x in (campaign.message_ids or "").split(",")
                    if x.strip()
                ]
                existing.append(str(sent_msg.message_id))
                campaign.message_ids = ",".join(existing[-MAX_TRACKED_MESSAGE_IDS:])

            logger.info(
                f"Successfully posted ad for campaign #{campaign_id} (user {user_id}) to {config.group_id}"
            )

            campaign.publications_left -= 1
            campaign.failure_count = 0

            if campaign.publications_left <= 0:
                campaign.is_active = False
                remove_campaign_job(campaign)

                try:
                    finish_msg = get_text(lang, "ad_finished", count=total_published)
                    await bot.send_message(user_id, finish_msg)
                except Exception as e:
                    logger.error(f"Failed to send finish msg: {e}")

            await session.commit()

        except Exception as e:
            # The commit above is inside this try, so the failure may be the
            # commit itself. Roll back first: writing to a session that is in a
            # pending-rollback state raises, which would lose the failure counter
            # as well and leave the job retrying forever.
            try:
                await session.rollback()

                # rollback() expires every instance in the session (unconditionally,
                # regardless of expire_on_commit), so touching the old `campaign`
                # object would trigger a lazy refresh SELECT and raise
                # MissingGreenlet under AsyncSession. Re-fetch instead.
                campaign = await session.get(Campaign, campaign_id)
                if campaign is None:
                    return

                campaign.failure_count = (campaign.failure_count or 0) + 1
                logger.error(
                    f"Failed to post ad #{campaign_id} to group {config.group_id} "
                    f"(failure {campaign.failure_count}/{MAX_CONSECUTIVE_FAILURES}): {e}. "
                    "Ensure the bot is an Administrator in the group with permission to post messages!"
                )

                if campaign.failure_count >= MAX_CONSECUTIVE_FAILURES:
                    # Stop retrying forever: an unstoppable job keeps the scheduler
                    # busy and rebuilds Telegram clients every interval until OOM.
                    campaign.is_active = False
                    remove_campaign_job(campaign)
                    logger.error(
                        f"Campaign #{campaign_id} deactivated after "
                        f"{campaign.failure_count} consecutive failures."
                    )

                await session.commit()
            except Exception:
                # If the failure counter cannot be persisted, the job would retry
                # forever again; log loudly rather than swallowing it silently.
                logger.exception(
                    f"Could not persist failure state for campaign #{campaign_id} "
                    f"(original error: {e})"
                )
            return


AD_BUTTON_TEXTS = [TEXTS[l]["ad_button"] for l in TEXTS]


@router.message(F.text.in_(AD_BUTTON_TEXTS))
async def start_ad_flow(message: Message, state: FSMContext):
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(message.from_user.id, session)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_text(lang, "back_btn"), callback_data="back_main"
                )
            ]
        ]
    )
    await message.answer(get_text(lang, "ask_count"), reply_markup=markup)
    await state.set_state(AdFlow.count)
    await state.update_data(lang=lang)


@router.callback_query(AdFlow.count, F.data == "back_main")
async def back_from_count(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ky")
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        get_text(lang, "welcome", price_per_ad=await get_price_per_ad()),
        reply_markup=main_keyboard(lang),
    )


@router.message(AdFlow.count)
async def process_count(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ky")

    if not message.text or not message.text.strip().isdigit():
        await message.answer(get_text(lang, "invalid_number"))
        return

    count = int(message.text.strip())
    if count <= 0:
        await message.answer(get_text(lang, "invalid_number"))
        return

    await state.update_data(count=count)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_text(lang, "int_1_min"), callback_data="int_1"
                ),
                InlineKeyboardButton(
                    text=get_text(lang, "int_3_min"), callback_data="int_3"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=get_text(lang, "int_5_min"), callback_data="int_5"
                ),
                InlineKeyboardButton(
                    text=get_text(lang, "int_10_min"), callback_data="int_10"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=get_text(lang, "int_20_min"), callback_data="int_20"
                ),
                InlineKeyboardButton(
                    text=get_text(lang, "int_40_min"), callback_data="int_40"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=get_text(lang, "back_btn"), callback_data="back_count"
                )
            ],
        ]
    )
    await message.answer(get_text(lang, "ask_interval"), reply_markup=markup)
    await state.set_state(AdFlow.interval)


@router.callback_query(AdFlow.interval, F.data == "back_count")
async def back_from_interval(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ky")
    await state.set_state(AdFlow.count)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_text(lang, "back_btn"), callback_data="back_main"
                )
            ]
        ]
    )
    await callback.message.delete()
    await callback.message.answer(get_text(lang, "ask_count"), reply_markup=markup)


@router.callback_query(AdFlow.interval, F.data.startswith("int_"))
async def process_interval(callback: CallbackQuery, state: FSMContext):
    interval = int(callback.data.split("_")[1])
    await state.update_data(interval=interval)

    data = await state.get_data()
    count = data["count"]
    lang = data.get("lang", "ky")
    price_per_ad = await get_price_per_ad()
    price = count * price_per_ad

    summary = get_text(
        lang, "summary", count=count, interval=interval, price=price,
        price_per_ad=price_per_ad,
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_text(lang, "pay"), callback_data="pay_yes"
                ),
                InlineKeyboardButton(
                    text=get_text(lang, "cancel"), callback_data="pay_no"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=get_text(lang, "back_btn"), callback_data="back_interval"
                )
            ],
        ]
    )

    await callback.message.edit_text(summary, reply_markup=markup)
    await state.set_state(AdFlow.confirm_payment)


@router.callback_query(AdFlow.confirm_payment, F.data == "back_interval")
async def back_from_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ky")
    await state.set_state(AdFlow.interval)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_text(lang, "int_1_min"), callback_data="int_1"
                ),
                InlineKeyboardButton(
                    text=get_text(lang, "int_3_min"), callback_data="int_3"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=get_text(lang, "int_5_min"), callback_data="int_5"
                ),
                InlineKeyboardButton(
                    text=get_text(lang, "int_10_min"), callback_data="int_10"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=get_text(lang, "int_20_min"), callback_data="int_20"
                ),
                InlineKeyboardButton(
                    text=get_text(lang, "int_40_min"), callback_data="int_40"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=get_text(lang, "back_btn"), callback_data="back_count"
                )
            ],
        ]
    )
    await callback.message.edit_text(
        get_text(lang, "ask_interval"), reply_markup=markup
    )


@router.callback_query(AdFlow.confirm_payment, F.data == "pay_no")
async def process_cancel_payment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ky")
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        get_text(lang, "welcome", price_per_ad=await get_price_per_ad()),
        reply_markup=main_keyboard(lang),
    )


def _notify_paid(user_id: int, lang: str) -> None:
    """Send the payment-confirmed message without blocking the caller.

    Split out so tests can replace it, and so a webhook is not held open
    waiting on the Telegram API.
    """
    _spawn(get_bot().send_message(user_id, get_text(lang, "payment_confirmed")))


async def _advance_to_content(user_id: int, lang: str) -> None:
    """Move a paid user to content entry and tell them, exactly once.

    Guarded on the user's current FSM state rather than the Payment row: the
    row says money arrived, the state says whether the user has been told.
    That makes this safe to call from the check button and from a duplicate
    webhook delivery alike -- including a webhook that lands before this
    process has a dispatcher, or after the user has already moved on.
    """
    state = get_fsm_context(user_id)
    if state is None:
        return
    if await state.get_state() != AdFlow.waiting_payment.state:
        return
    await state.set_state(AdFlow.content)
    _notify_paid(user_id, lang)


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
            lang = await get_user_lang(payment.user_id, session)
            await _advance_to_content(payment.user_id, lang)
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
            lang = await get_user_lang(payment.user_id, session)
            await _advance_to_content(payment.user_id, lang)
            return True

        payment.status = pay_status
        # payments.updated_at is naive UTC, matching every other timestamp in
        # this schema and what the admin panel's format_local() assumes; strip
        # tzinfo rather than storing an aware value.
        payment.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()

        if pay_status != "COMPLETED":
            return False

        lang = await get_user_lang(user_id, session)

    await _advance_to_content(user_id, lang)
    logger.info(f"Payment {payment_id} confirmed for user {user_id}")
    return True


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


@router.message(AdFlow.content)
async def process_content(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    lang = data.get("lang", "ky")

    content_text = message.text or message.caption or ""
    content_photo = message.photo[-1].file_id if message.photo else None

    photo_bytes = None
    if message.photo:
        try:
            photo_file = await bot.get_file(message.photo[-1].file_id)
            if photo_file.file_path:
                file_stream = await bot.download_file(photo_file.file_path)
                if file_stream:
                    photo_bytes = file_stream.read()
        except Exception as e:
            logger.debug(f"Failed to fetch photo for moderation: {e}")

    # Fast multi-stage moderation
    is_valid, reason = await moderate_full_content(content_text, photo_bytes)
    if not is_valid:
        if reason == "links_not_allowed":
            await message.answer(get_text(lang, "content_links_blocked"))
        else:
            await message.answer(get_text(lang, "content_18_blocked"))
        return

    await state.update_data(content_text=content_text, content_photo=content_photo)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_text(lang, "correct_start"), callback_data="content_ok"
                )
            ],
            [
                InlineKeyboardButton(
                    text=get_text(lang, "rewrite"), callback_data="content_redo"
                )
            ],
        ]
    )

    await message.answer(get_text(lang, "confirm_content"))

    if content_photo:
        await message.answer_photo(
            content_photo, caption=content_text, reply_markup=markup
        )
    else:
        safe_text = content_text if content_text else "."
        await message.answer(safe_text, reply_markup=markup)

    await state.set_state(AdFlow.confirm_content)


@router.callback_query(AdFlow.confirm_content, F.data == "content_redo")
async def process_content_redo(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ky")
    await callback.message.delete()
    await callback.message.answer(get_text(lang, "payment_confirmed"))
    await state.set_state(AdFlow.content)


@router.callback_query(AdFlow.confirm_content, F.data == "content_ok")
async def process_content_ok(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ky")

    async with AsyncSessionLocal() as session:
        campaign = Campaign(
            user_id=callback.from_user.id,
            publications_total=data["count"],
            publications_left=data["count"],
            interval_minutes=data["interval"],
            content_text=data.get("content_text"),
            content_photo=data.get("content_photo"),
            is_active=True,
            price_paid=data.get("price", 0.0),
        )
        session.add(campaign)
        await session.commit()

        payment_db_id = data.get("payment_db_id")
        if payment_db_id:
            payment = await session.get(Payment, payment_db_id)
            if payment:
                payment.campaign_id = campaign.id
                payment.updated_at = datetime.now(UTC).replace(tzinfo=None)
                await session.commit()

        campaign.job_id = schedule_campaign(campaign)
        session.add(campaign)
        await session.commit()

    _spawn(post_ad(campaign.user_id, campaign.id))

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_text(lang, "stop_ad"), callback_data=f"stop_{campaign.id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=get_text(lang, "new_ad"), callback_data="start_new_ad"
                )
            ],
        ]
    )

    await callback.message.delete()
    await callback.message.answer(
        get_text(lang, "accepted", count=data["count"], interval=data["interval"]),
        reply_markup=markup,
    )

    await state.clear()


@router.callback_query(F.data.startswith("stop_"))
async def stop_campaign(callback: CallbackQuery):
    campaign_id = int(callback.data.split("_")[1])
    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        lang = await get_user_lang(callback.from_user.id, session)

        if (
            campaign
            and campaign.user_id == callback.from_user.id
            and campaign.is_active
        ):
            campaign.is_active = False
            remove_campaign_job(campaign)
            await session.commit()
            await callback.answer(get_text(lang, "ad_stopped"), show_alert=True)

            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=get_text(lang, "start_ad"),
                            callback_data=f"resume_{campaign.id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=get_text(lang, "new_ad"), callback_data="start_new_ad"
                        )
                    ],
                ]
            )

            stop_str = get_text(lang, "ad_stopped")
            try:
                msg_text = (
                    callback.message.caption
                    if callback.message.photo
                    else callback.message.text
                )
                new_text = f"{msg_text}\n\n{stop_str}" if msg_text else stop_str
                if callback.message.photo:
                    await callback.message.edit_caption(
                        caption=new_text, reply_markup=markup
                    )
                else:
                    await callback.message.edit_text(text=new_text, reply_markup=markup)
            except Exception:
                await callback.message.edit_reply_markup(reply_markup=markup)
        else:
            await callback.answer(get_text(lang, "error_or_stopped"), show_alert=True)


@router.callback_query(F.data.startswith("resume_"))
async def resume_campaign(callback: CallbackQuery):
    campaign_id = int(callback.data.split("_")[1])
    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        lang = await get_user_lang(callback.from_user.id, session)

        if (
            campaign
            and campaign.user_id == callback.from_user.id
            and not campaign.is_active
        ):
            if campaign.publications_left <= 0:
                await callback.answer(
                    get_text(lang, "ad_finished", count=campaign.publications_total),
                    show_alert=True,
                )
                return

            campaign.is_active = True
            campaign.failure_count = 0
            campaign.job_id = schedule_campaign(campaign)
            await session.commit()

            await callback.answer(get_text(lang, "ad_started"), show_alert=True)

            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=get_text(lang, "stop_ad"),
                            callback_data=f"stop_{campaign.id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=get_text(lang, "new_ad"), callback_data="start_new_ad"
                        )
                    ],
                ]
            )

            stop_str = get_text(lang, "ad_stopped")
            try:
                msg_text = (
                    callback.message.caption
                    if callback.message.photo
                    else callback.message.text
                )
                if msg_text:
                    new_text = (
                        msg_text.replace(f"\n\n{stop_str}", "")
                        .replace(f"\n{stop_str}", "")
                        .replace(stop_str, "")
                    )
                else:
                    new_text = ""

                if callback.message.photo:
                    await callback.message.edit_caption(
                        caption=new_text, reply_markup=markup
                    )
                else:
                    await callback.message.edit_text(text=new_text, reply_markup=markup)
            except Exception:
                await callback.message.edit_reply_markup(reply_markup=markup)
        else:
            await callback.answer(get_text(lang, "error_or_stopped"), show_alert=True)


@router.callback_query(F.data == "start_new_ad")
async def start_new_ad_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(callback.from_user.id, session)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_text(lang, "back_btn"), callback_data="back_main"
                )
            ]
        ]
    )
    await state.update_data(lang=lang)
    await state.set_state(AdFlow.count)
    await callback.message.answer(get_text(lang, "ask_count"), reply_markup=markup)
    await callback.answer()


@router.callback_query()
async def fallback_stale_callback(callback: CallbackQuery):
    """Handles any outdated inline buttons when FSM session in RAM was cleared after redeploy."""
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(callback.from_user.id, session)
    try:
        if lang == "ru":
            msg = "⚠️ Сессия была обновлена после перезагрузки. Нажмите /start или кнопку меню, чтобы продолжить."
        else:
            msg = "⚠️ Бот жаңыланды. Улантуу үчүн /start басыңыз же менюдагы баскычты тандаңыз."
        await callback.answer(msg, show_alert=True)
    except Exception:
        pass
