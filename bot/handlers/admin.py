from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import case, func, or_, select

from bot.config import config
from bot.database.db import AsyncSessionLocal
from bot.database.models import Campaign, User
from bot.services.settings_store import get_price_per_ad

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

        # Revenue must match the dashboard's own figure: prefer each campaign's
        # actual price_paid, and only fall back to publications * current price
        # for legacy rows that predate that column. Using current price for
        # every row (the old behavior) drifts from the dashboard the moment
        # the price is ever changed.
        price = await get_price_per_ad()
        spent_expr = case(
            (
                or_(Campaign.price_paid.is_(None), Campaign.price_paid == 0),
                func.coalesce(Campaign.publications_total, 0) * price,
            ),
            else_=Campaign.price_paid,
        )
        rev_result = await session.execute(
            select(func.coalesce(func.sum(spent_expr), 0.0))
        )
        total_revenue = round(rev_result.scalar() or 0.0, 2)

    report = (
        f"📊 <b>Admin Dashboard</b>\n\n"
        f"👥 Total Users: {total_users}\n"
        f"📢 Active Ads: {active_campaigns}\n"
        f"📈 Total Ads Created: {total_campaigns}\n"
        f"💰 Total Revenue: {total_revenue} сом"
    )

    await message.answer(report, parse_mode="HTML")
