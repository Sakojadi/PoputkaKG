import logging
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.db import AsyncSessionLocal
from bot.database.models import Campaign
from bot.locales.translations import get_text, TEXTS
from bot.handlers.start import get_user_lang, main_keyboard
from bot.services.payment import generate_xpay_link, check_xpay_payment
from bot.services.scheduler import scheduler
from bot.services.moderation import moderate_full_content
from bot.config import config

logger = logging.getLogger(__name__)
router = Router()

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

        lang = await get_user_lang(user_id, session)
        total_published = campaign.publications_total
        
        bot = Bot(token=config.bot_token)
        try:
            if campaign.content_photo:
                await bot.send_photo(chat_id=config.group_id, photo=campaign.content_photo, caption=campaign.content_text or "")
            else:
                await bot.send_message(chat_id=config.group_id, text=campaign.content_text)
            
            logger.info(f"Successfully posted ad for campaign {campaign_id} to {config.group_id}")
            
            campaign.publications_left -= 1
            
            if campaign.publications_left <= 0:
                campaign.is_active = False
                if campaign.job_id:
                    try:
                        scheduler.remove_job(campaign.job_id)
                    except Exception:
                        pass
                
                try:
                    finish_msg = get_text(lang, "ad_finished", count=total_published)
                    await bot.send_message(user_id, finish_msg)
                except Exception as e:
                    logger.error(f"Failed to send finish msg: {e}")

            await session.commit()
            
        except Exception as e:
            logger.error(f"Failed to send ad: {e}. Target chat_id: {config.group_id}")
            return
        finally:
            await bot.session.close()

AD_BUTTON_TEXTS = [TEXTS[l]["ad_button"] for l in TEXTS]

@router.message(F.text.in_(AD_BUTTON_TEXTS))
async def start_ad_flow(message: Message, state: FSMContext):
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(message.from_user.id, session)
        
    markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=get_text(lang, "back_btn"))]], resize_keyboard=True)
    await message.answer(get_text(lang, "ask_count"), reply_markup=markup)
    await state.set_state(AdFlow.count)
    await state.update_data(lang=lang)

@router.message(AdFlow.count, F.text.in_([TEXTS[l].get("back_btn", "") for l in TEXTS]))
async def back_from_count(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get('lang', 'ky')
    await state.clear()
    await message.answer(get_text(lang, "welcome"), reply_markup=main_keyboard(lang))


@router.message(AdFlow.count)
async def process_count(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get('lang', 'ky')
    
    if not message.text or not message.text.strip().isdigit():
        await message.answer(get_text(lang, "invalid_number"))
        return
        
    count = int(message.text.strip())
    if count <= 0:
        await message.answer(get_text(lang, "invalid_number"))
        return
        
    await state.update_data(count=count)
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_text(lang, "int_1_min"), callback_data="int_1"), 
         InlineKeyboardButton(text=get_text(lang, "int_3_min"), callback_data="int_3")],
        [InlineKeyboardButton(text=get_text(lang, "int_5_min"), callback_data="int_5"), 
         InlineKeyboardButton(text=get_text(lang, "int_10_min"), callback_data="int_10")],
        [InlineKeyboardButton(text=get_text(lang, "int_20_min"), callback_data="int_20"), 
         InlineKeyboardButton(text=get_text(lang, "int_40_min"), callback_data="int_40")],
        [InlineKeyboardButton(text=get_text(lang, "back_btn"), callback_data="back_count")]
    ])
    await message.answer(get_text(lang, "ask_interval"), reply_markup=markup)
    await state.set_state(AdFlow.interval)

@router.callback_query(AdFlow.interval, F.data == "back_count")
async def back_from_interval(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get('lang', 'ky')
    await state.set_state(AdFlow.count)
    markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=get_text(lang, "back_btn"))]], resize_keyboard=True)
    await callback.message.delete()
    await callback.message.answer(get_text(lang, "ask_count"), reply_markup=markup)


@router.callback_query(AdFlow.interval, F.data.startswith("int_"))
async def process_interval(callback: CallbackQuery, state: FSMContext):
    interval = int(callback.data.split("_")[1])
    await state.update_data(interval=interval)
    
    data = await state.get_data()
    count = data['count']
    lang = data.get('lang', 'ky')
    price = count * 1.0
    
    summary = get_text(lang, "summary", count=count, interval=interval, price=price)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_text(lang, "pay"), callback_data="pay_yes"), 
         InlineKeyboardButton(text=get_text(lang, "cancel"), callback_data="pay_no")],
        [InlineKeyboardButton(text=get_text(lang, "back_btn"), callback_data="back_interval")]
    ])
    
    await callback.message.edit_text(summary, reply_markup=markup)
    await state.set_state(AdFlow.confirm_payment)

@router.callback_query(AdFlow.confirm_payment, F.data == "back_interval")
async def back_from_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get('lang', 'ky')
    await state.set_state(AdFlow.interval)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_text(lang, "int_1_min"), callback_data="int_1"), 
         InlineKeyboardButton(text=get_text(lang, "int_3_min"), callback_data="int_3")],
        [InlineKeyboardButton(text=get_text(lang, "int_5_min"), callback_data="int_5"), 
         InlineKeyboardButton(text=get_text(lang, "int_10_min"), callback_data="int_10")],
        [InlineKeyboardButton(text=get_text(lang, "int_20_min"), callback_data="int_20"), 
         InlineKeyboardButton(text=get_text(lang, "int_40_min"), callback_data="int_40")],
        [InlineKeyboardButton(text=get_text(lang, "back_btn"), callback_data="back_count")]
    ])
    await callback.message.edit_text(get_text(lang, "ask_interval"), reply_markup=markup)


@router.callback_query(AdFlow.confirm_payment, F.data == "pay_no")
async def process_cancel_payment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get('lang', 'ky')
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(get_text(lang, "welcome"), reply_markup=main_keyboard(lang))

@router.callback_query(AdFlow.confirm_payment, F.data == "pay_yes")
async def process_payment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    count = data['count']
    lang = data.get('lang', 'ky')
    price = count * 1.0
    
    link, payment_id = await generate_xpay_link(price)
    await state.update_data(payment_id=payment_id)
    
    msg_text = get_text(lang, "payment_info", price=price, link=link)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_text(lang, "check_payment"), callback_data="check_pay")]
    ])
    
    await callback.message.edit_text(msg_text, reply_markup=markup)
    await state.set_state(AdFlow.waiting_payment)

@router.callback_query(AdFlow.waiting_payment, F.data == "check_pay")
async def check_payment_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get('lang', 'ky')
    payment_id = data['payment_id']
    
    is_paid = await check_xpay_payment(payment_id)
    if not is_paid:
        await callback.answer(get_text(lang, "payment_not_found"), show_alert=True)
        return
        
    await callback.message.delete()
    await callback.message.answer(get_text(lang, "payment_confirmed"))
    await state.set_state(AdFlow.content)

@router.message(AdFlow.content)
async def process_content(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    lang = data.get('lang', 'ky')
    
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
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_text(lang, "correct_start"), callback_data="content_ok")],
        [InlineKeyboardButton(text=get_text(lang, "back_btn"), callback_data="content_redo")]
    ])
    
    preview_text = f"{get_text(lang, 'confirm_content')}\n\n{content_text or ''}"
    if content_photo:
        await message.answer_photo(content_photo, caption=preview_text, reply_markup=markup)
    else:
        await message.answer(preview_text, reply_markup=markup)
        
    await state.set_state(AdFlow.confirm_content)

@router.callback_query(AdFlow.confirm_content, F.data == "content_redo")
async def process_content_redo(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get('lang', 'ky')
    await callback.message.delete()
    await callback.message.answer(get_text(lang, "payment_confirmed"))
    await state.set_state(AdFlow.content)

@router.callback_query(AdFlow.confirm_content, F.data == "content_ok")
async def process_content_ok(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get('lang', 'ky')
    
    async with AsyncSessionLocal() as session:
        campaign = Campaign(
            user_id=callback.from_user.id,
            publications_total=data['count'],
            publications_left=data['count'],
            interval_minutes=data['interval'],
            content_text=data.get('content_text'),
            content_photo=data.get('content_photo'),
            is_active=True
        )
        session.add(campaign)
        await session.commit()
        
        job = scheduler.add_job(
            post_ad, 
            'interval', 
            minutes=campaign.interval_minutes,
            args=[callback.from_user.id, campaign.id]
        )
        
        campaign.job_id = job.id
        session.add(campaign)
        await session.commit()

    import asyncio
    asyncio.create_task(post_ad(callback.from_user.id, campaign.id))
        
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_text(lang, "stop_ad"), callback_data=f"stop_{campaign.id}")]
    ])
    
    await callback.message.delete()
    await callback.message.answer(
        get_text(lang, "accepted", count=data['count'], interval=data['interval']),
        reply_markup=markup
    )
    await state.clear()

@router.callback_query(F.data.startswith("stop_"))
async def stop_campaign(callback: CallbackQuery):
    campaign_id = int(callback.data.split("_")[1])
    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        lang = await get_user_lang(callback.from_user.id, session)
        
        if campaign and campaign.user_id == callback.from_user.id and campaign.is_active:
            campaign.is_active = False
            if campaign.job_id:
                try:
                    scheduler.remove_job(campaign.job_id)
                except Exception:
                    pass
            await session.commit()
            await callback.answer(get_text(lang, "ad_stopped"), show_alert=True)
            await callback.message.edit_reply_markup(reply_markup=None)
        else:
            await callback.answer(get_text(lang, "error_or_stopped"), show_alert=True)
