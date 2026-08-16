from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from bot.config import config
from bot.database.db import AsyncSessionLocal
from bot.database.models import Campaign, User

router = Router()


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in config.admin_ids:
        return  # Silently ignore non-admins

    async with AsyncSessionLocal() as session:
        # Get total users
        users_count_result = await session.execute(
            select(func.count()).select_from(User)
        )
        total_users = users_count_result.scalar() or 0

        # Get active campaigns
        active_c_result = await session.execute(
            select(func.count()).select_from(Campaign).where(Campaign.is_active == True)
        )
        active_campaigns = active_c_result.scalar() or 0

        # Get total campaigns ever
        total_c_result = await session.execute(
            select(func.count()).select_from(Campaign)
        )
        total_campaigns = total_c_result.scalar() or 0

        # Calculate revenue (1 som per publication)
        rev_result = await session.execute(
            select(func.sum(Campaign.publications_total))
        )
        total_revenue = rev_result.scalar() or 0

    report = (
        f"📊 <b>Admin Dashboard</b>\n\n"
        f"👥 Total Users: {total_users}\n"
        f"📢 Active Ads: {active_campaigns}\n"
        f"📈 Total Ads Created: {total_campaigns}\n"
        f"💰 Total Revenue: {total_revenue} сом"
    )

    await message.answer(report, parse_mode="HTML")
