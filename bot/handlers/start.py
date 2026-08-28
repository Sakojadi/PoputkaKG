from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.db import AsyncSessionLocal
from bot.database.models import Payment, User
from bot.locales.translations import TEXTS, get_text
from bot.services.settings_store import get_price_per_ad

router = Router()


async def get_user_lang(user_id: int, session: AsyncSession) -> str:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user and user.language:
        return user.language
    new_user = User(id=user_id, language="ky")
    session.add(new_user)
    await session.commit()
    return "ky"


def main_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=get_text(lang, "ad_button"))],
            [
                KeyboardButton(text=get_text(lang, "help_button")),
                KeyboardButton(text=get_text(lang, "price_button")),
            ],
            [KeyboardButton(text=get_text(lang, "lang_button"))],
        ],
        resize_keyboard=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(user_id, session)
        result = await session.execute(
            select(Payment)
            .where(
                Payment.user_id == user_id,
                Payment.status == "COMPLETED",
                Payment.campaign_id.is_(None),
            )
            .order_by(Payment.created_at.desc())
        )
        stranded_payment = result.scalars().first()
        # Payments created before publications_count/interval_minutes existed
        # (or any row missing them for another reason) don't carry enough to
        # rebuild a Campaign -- recovering into AdFlow.content would set
        # count/interval to None and break campaign creation later. Treat
        # those as still needing manual admin follow-up, same as before.
        if stranded_payment is not None and (
            stranded_payment.publications_count is None
            or stranded_payment.interval_minutes is None
        ):
            stranded_payment = None

    if stranded_payment is not None:
        # Payment went through but the FSM state (in-memory) that would have
        # walked the user to "send your post" was lost -- almost always a
        # process restart between payment and content submission. The
        # Payment row is durable and carries everything needed to resume
        # (see Payment.publications_count/interval_minutes/lang), so recover
        # here instead of leaving the user stuck on the generic welcome text.
        from bot.handlers.ad_flow import AdFlow  # local import: ad_flow imports from this module

        recovered_lang = stranded_payment.lang or lang
        await state.set_state(AdFlow.content)
        await state.update_data(
            payment_db_id=stranded_payment.id,
            lang=recovered_lang,
            count=stranded_payment.publications_count,
            interval=stranded_payment.interval_minutes,
        )
        await message.answer(get_text(recovered_lang, "payment_confirmed"))
        return

    await message.answer(
        get_text(lang, "welcome", price_per_ad=await get_price_per_ad()),
        reply_markup=main_keyboard(lang),
    )


# Helper list of all language button texts across all languages
LANG_BUTTON_TEXTS = [TEXTS[l]["lang_button"] for l in TEXTS]
HELP_BUTTON_TEXTS = [TEXTS[l]["help_button"] for l in TEXTS]
PRICE_BUTTON_TEXTS = [TEXTS[l]["price_button"] for l in TEXTS]


@router.message(F.text.in_(LANG_BUTTON_TEXTS))
@router.message(Command("language"))
async def change_language(message: Message):
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(message.from_user.id, session)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🇰🇬 Кыргызча", callback_data="lang_ky")],
            [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
        ]
    )
    await message.answer(get_text(lang, "choose_lang"), reply_markup=markup)


@router.callback_query(F.data.startswith("lang_"))
async def process_lang_change(callback: CallbackQuery):
    new_lang = callback.data.split("_")[1]
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user:
            user.language = new_lang
            await session.commit()
        else:
            user = User(id=callback.from_user.id, language=new_lang)
            session.add(user)
            await session.commit()

    await callback.message.delete()
    await callback.message.answer(
        get_text(new_lang, "welcome", price_per_ad=await get_price_per_ad()),
        reply_markup=main_keyboard(new_lang),
    )
    await callback.answer(get_text(new_lang, "lang_changed"))


@router.message(F.text.in_(HELP_BUTTON_TEXTS))
@router.message(Command("help"))
async def cmd_help(message: Message):
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(message.from_user.id, session)
    await message.answer(
        get_text(lang, "help_text", price_per_ad=await get_price_per_ad())
    )


@router.message(F.text.in_(PRICE_BUTTON_TEXTS))
@router.message(Command("price"))
async def cmd_price(message: Message):
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(message.from_user.id, session)
    await message.answer(
        get_text(lang, "price_text", price_per_ad=await get_price_per_ad())
    )
