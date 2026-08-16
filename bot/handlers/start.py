from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from bot.database.models import User
from bot.database.db import AsyncSessionLocal
from bot.locales.translations import get_text

router = Router()

async def get_user_lang(user_id: int, session: AsyncSession) -> str:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        return user.language
    new_user = User(id=user_id, language='ky')
    session.add(new_user)
    await session.commit()
    return 'ky'

def main_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=get_text(lang, "ad_button"))],
            [KeyboardButton(text=get_text(lang, "help_button")), KeyboardButton(text=get_text(lang, "price_button"))],
            [KeyboardButton(text=get_text(lang, "lang_button"))]
        ],
        resize_keyboard=True
    )

@router.message(CommandStart())
async def cmd_start(message: Message):
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(message.from_user.id, session)
        await message.answer(
            get_text(lang, "welcome"),
            reply_markup=main_keyboard(lang)
        )

@router.message(F.text.in_(['🌐 Тилди өзгөртүү', '🌐 Сменить язык']))
@router.message(Command("language"))
async def change_language(message: Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇰🇬 Кыргызча", callback_data="lang_ky")],
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")]
    ])
    await message.answer("Тилди тандаңыз / Выберите язык:", reply_markup=markup)

@router.callback_query(F.data.startswith("lang_"))
async def process_lang_change(callback: CallbackQuery):
    new_lang = callback.data.split("_")[1]
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.id == callback.from_user.id))
        user = result.scalar_one_or_none()
        if user:
            user.language = new_lang
            await session.commit()
    
    await callback.message.delete()
    await callback.message.answer(
        get_text(new_lang, "welcome"),
        reply_markup=main_keyboard(new_lang)
    )
    await callback.answer()

@router.message(F.text.in_(['❓ Жардам', '❓ Помощь']))
async def cmd_help(message: Message):
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(message.from_user.id, session)
    if lang == 'ky':
        text = "Жардам:\nБул бот аркылуу группага реклама чыгара аласыз. Реклама берүү баскычын басып, кадамдарды аткарыңыз."
    else:
        text = "Помощь:\nС помощью бота вы можете публиковать рекламу в группе. Нажмите кнопку 'Дать рекламу' и следуйте шагам."
    await message.answer(text)

@router.message(F.text.in_(['💰 Баасы', '💰 Цены']))
async def cmd_price(message: Message):
    async with AsyncSessionLocal() as session:
        lang = await get_user_lang(message.from_user.id, session)
    if lang == 'ky':
        text = "Баасы: 1 публикация — 1.0 сом."
    else:
        text = "Цена: 1 публикация — 1.0 сом."
    await message.answer(text)
