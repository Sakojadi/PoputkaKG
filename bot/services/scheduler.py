import logging

from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from bot.config import config

logger = logging.getLogger(__name__)

jobstores = {
    "default": SQLAlchemyJobStore(
        url=config.database_url.replace("+aiosqlite", "").replace("+asyncpg", "")
    )
}

# Without an explicit grace time APScheduler silently drops any job that is more
# than 1 second late, so every restart loses paid publications.
job_defaults = {
    "coalesce": True,
    "max_instances": 1,
    "misfire_grace_time": 300,
}

scheduler = AsyncIOScheduler(jobstores=jobstores, job_defaults=job_defaults)


def job_id_for(campaign_id: int) -> str:
    """Deterministic job id, so a campaign can never own more than one job."""
    return f"campaign_{campaign_id}"


def schedule_campaign(campaign) -> str:
    """Create (or replace) the recurring job for a campaign."""
    from bot.handlers.ad_flow import post_ad

    job_id = job_id_for(campaign.id)
    scheduler.add_job(
        post_ad,
        "interval",
        minutes=campaign.interval_minutes,
        args=[campaign.user_id, campaign.id],
        id=job_id,
        replace_existing=True,
    )
    return job_id


def remove_campaign_job(campaign) -> None:
    """Remove a campaign's job, including one created under the old random id."""
    for job_id in {job_id_for(campaign.id), getattr(campaign, "job_id", None)}:
        if not job_id:
            continue
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            pass
        except Exception:
            logger.exception("Failed to remove scheduler job %s", job_id)


def group_post_job_id_for(post_id: int) -> str:
    """Deterministic job id, so a group post can never own more than one job."""
    return f"group_post_{post_id}"


def schedule_group_post(post) -> str:
    """Create (or replace) the recurring job for a repeating group post."""
    from bot.services.group_posts import post_group_message

    job_id = group_post_job_id_for(post.id)
    scheduler.add_job(
        post_group_message,
        "interval",
        minutes=post.interval_minutes,
        args=[post.id],
        id=job_id,
        replace_existing=True,
    )
    return job_id


def remove_group_post_job(post) -> None:
    """Remove a group post's job, including one created under an earlier id."""
    for job_id in {group_post_job_id_for(post.id), getattr(post, "job_id", None)}:
        if not job_id:
            continue
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            pass
        except Exception:
            logger.exception("Failed to remove scheduler job %s", job_id)


def start_scheduler():
    scheduler.start()


async def sync_jobs_with_db() -> None:
    """Reconcile the persisted jobstore against the campaigns table on boot.

    The campaigns table is the source of truth. Anything in the jobstore that
    does not correspond to an active campaign is garbage left behind by a crash,
    a failed removal, or the old random-job-id scheme, and is dropped here.
    """
    from bot.database.db import AsyncSessionLocal
    from bot.database.models import Campaign, GroupPost

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Campaign).where(
                Campaign.is_active.is_(True), Campaign.publications_left > 0
            )
        )
        active = {c.id: c for c in result.scalars().all()}

        gp_result = await session.execute(
            select(GroupPost).where(
                GroupPost.is_active.is_(True), GroupPost.repeats_left > 0
            )
        )
        active_posts = {p.id: p for p in gp_result.scalars().all()}

        wanted = {job_id_for(cid) for cid in active} | {
            group_post_job_id_for(pid) for pid in active_posts
        }
        existing = {job.id for job in scheduler.get_jobs()}

        removed = 0
        for job_id in existing - wanted:
            try:
                scheduler.remove_job(job_id)
                removed += 1
            except JobLookupError:
                pass
            except Exception:
                logger.exception("Failed to drop orphaned job %s", job_id)

        added = 0
        for campaign in active.values():
            job_id = job_id_for(campaign.id)
            if job_id not in existing:
                schedule_campaign(campaign)
                added += 1
            if campaign.job_id != job_id:
                campaign.job_id = job_id

        for post in active_posts.values():
            job_id = group_post_job_id_for(post.id)
            if job_id not in existing:
                schedule_group_post(post)
                added += 1
            if post.job_id != job_id:
                post.job_id = job_id

        await session.commit()

    logger.info(
        "Scheduler reconciled: %s active campaigns, %s active group posts, "
        "%s orphaned jobs dropped, %s jobs restored.",
        len(active),
        len(active_posts),
        removed,
        added,
    )
