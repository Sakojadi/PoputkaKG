from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.db import AsyncSessionLocal
from bot.database.models import Campaign
from bot.locales.translations import get_text
from bot.handlers.start import get_user_lang
from bot.services.payment import generate_xpay_link, check_xpay_payment
from bot.services.scheduler import scheduler
from bot.config import config

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
        
        bot = Bot(token=config.bot_token)
        try:
            if campaign.content_photo:
                await bot.send_photo(chat_id=config.group_id, photo=campaign.content_photo, caption=campaign.content_text or "")
            else:
                await bot.send_message(chat_id=config.group_id, text=campaign.content_text)
        except Exception as e:
            print(f"Failed to send ad: {e}")
        finally:
            await bot.session.close()

        campaign.publications_left -= 1
        await session.commit()

        if campaign.publications_left <= 0:
            campaign.is_active = False
            await session.commit()
            if campaign.job_id:
                try:
                    scheduler.remove_job(campaign.job_id)
                except:
                    pass
            
            # Send completion msg in a new bot session
            bot2 = Bot(token=config.bot_token)
            try:
                await bot2.send_message(chat_id=user_id, text=get_text(lang, "ad_finished"))
            except:
                pass
            finally:
                await bot2.session.close()

@router.message(F.text.in_(['📢 Реклама берем', '📢 Дать рекламу']))
async def start_ad_flow(message: Message, state: FSMContext):
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(message.from_user.id, session)
        
    await message.answer(get_text(lang, "ask_count"), reply_markup=ReplyKeyboardRemove())
    await state.set_state(AdFlow.count)
    await state.update_data(lang=lang)

@router.message(AdFlow.count)
async def process_count(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data['lang']
    
    if not message.text.isdigit():
        await message.answer(get_text(lang, "invalid_number"))
        return
        
    count = int(message.text)
    if count <= 0:
        await message.answer(get_text(lang, "invalid_number"))
        return
        
    await state.update_data(count=count)
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 мүнөт/мин", callback_data="int_1"), InlineKeyboardButton(text="3 мүнөт/мин", callback_data="int_3")],
        [InlineKeyboardButton(text="5 мүнөт/мин", callback_data="int_5"), InlineKeyboardButton(text="10 мүнөт/мин", callback_data="int_10")],
        [InlineKeyboardButton(text="20 мүнөт/мин", callback_data="int_20"), InlineKeyboardButton(text="40 мүнөт/мин", callback_data="int_40")]
    ])
    await message.answer(get_text(lang, "ask_interval"), reply_markup=markup)
    await state.set_state(AdFlow.interval)

@router.callback_query(AdFlow.interval, F.data.startswith("int_"))
async def process_interval(callback: CallbackQuery, state: FSMContext):
    interval = int(callback.data.split("_")[1])
    await state.update_data(interval=interval)
    
    data = await state.get_data()
    count = data['count']
    lang = data['lang']
    price = count * 1.0
    
    summary = get_text(lang, "summary", count=count, interval=interval, price=price)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_text(lang, "pay"), callback_data="pay_yes"), 
         InlineKeyboardButton(text=get_text(lang, "cancel"), callback_data="pay_no")]
    ])
    
    await callback.message.edit_text(summary, reply_markup=markup)
    await state.set_state(AdFlow.confirm_payment)

@router.callback_query(AdFlow.confirm_payment, F.data == "pay_no")
async def process_cancel_payment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data['lang']
    from bot.handlers.start import main_keyboard
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(get_text(lang, "welcome"), reply_markup=main_keyboard(lang))

@router.callback_query(AdFlow.confirm_payment, F.data == "pay_yes")
async def process_payment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    count = data['count']
    lang = data['lang']
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
    lang = data['lang']
    payment_id = data['payment_id']
    
    is_paid = await check_xpay_payment(payment_id)
    if not is_paid:
        await callback.answer(get_text(lang, "payment_not_found"), show_alert=True)
        return
        
    await callback.message.delete()
    await callback.message.answer(get_text(lang, "payment_confirmed"))
    await state.set_state(AdFlow.content)

@router.message(AdFlow.content)
async def process_content(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data['lang']
    
    content_text = message.text or message.caption
    content_photo = message.photo[-1].file_id if message.photo else None
    
    await state.update_data(content_text=content_text, content_photo=content_photo)
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_text(lang, "correct_start"), callback_data="content_ok")],
        [InlineKeyboardButton(text=get_text(lang, "rewrite"), callback_data="content_redo")]
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
    lang = data['lang']
    await callback.message.delete()
    await callback.message.answer(get_text(lang, "payment_confirmed"))
    await state.set_state(AdFlow.content)

@router.callback_query(AdFlow.confirm_content, F.data == "content_ok")
async def process_content_ok(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    lang = data['lang']
    
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
        await session.flush()
        
        job = scheduler.add_job(
            post_ad, 
            'interval', 
            minutes=campaign.interval_minutes, 
            args=[callback.from_user.id, campaign.id]
        )
        campaign.job_id = job.id
        await session.commit()
        
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_text(lang, "stop_ad"), callback_data=f"stop_{campaign.id}")]
    ])
    
    from bot.handlers.start import main_keyboard
    await callback.message.delete()
    await callback.message.answer(
        get_text(lang, "accepted", count=data['count'], interval=data['interval']),
        reply_markup=markup
    )
    await callback.message.answer(get_text(lang, "welcome"), reply_markup=main_keyboard(lang))
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
                except:
                    pass
            await session.commit()
            await callback.answer(get_text(lang, "ad_stopped"), show_alert=True)
            await callback.message.edit_reply_markup(reply_markup=None)
        else:
            await callback.answer("Error or already stopped.")
