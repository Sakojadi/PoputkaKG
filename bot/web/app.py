import asyncio
import hashlib
import json
import logging
import math
import os
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import asc, delete, desc, func, or_, select
from starlette.middleware.sessions import SessionMiddleware

from bot.config import config
from bot.database.db import AsyncSessionLocal
from bot.database.models import Campaign, GroupPost, Payment, User
from bot.handlers.ad_flow import _spawn, confirm_payment
from bot.services.group_posts import build_group_post_markup
from bot.services.moderation import BANNED_WORDS
from bot.services.payments import is_mock
from bot.services.scheduler import (
    remove_campaign_job,
    remove_group_post_job,
    schedule_campaign,
    schedule_group_post,
)
from bot.services.settings_store import get_setting, set_settings
from bot.services.tg import get_bot
from bot.services.xpay import WEBHOOK_PATH

logger = logging.getLogger(__name__)

app = FastAPI(title="Poputka Admin Panel", docs_url=None, redoc_url=None)

# Session middleware for cookie-based authentication
app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key,
    max_age=3600 * 24 * 7,  # 7 days
    https_only=True,
)

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)

# Timestamps are stored as naive UTC; the panel is operated from Kyrgyzstan, so
# everything the admin sees (and the "today" filter) works in Bishkek local time.
try:
    LOCAL_TZ = ZoneInfo("Asia/Bishkek")
except ZoneInfoNotFoundError:  # slim images without the tzdata package
    # Kyrgyzstan has been a fixed UTC+6 with no DST since 2005.
    LOCAL_TZ = timezone(timedelta(hours=6), "Asia/Bishkek")


def format_local(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Render a stored (naive UTC) timestamp in Bishkek local time."""
    if not value:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TZ).strftime(fmt)


templates.env.filters["localdt"] = format_local


# Helper dependency to check login
def require_admin(request: Request):
    if not request.session.get("admin_logged_in"):
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/admin/login"},
        )
    return True


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if (
        exc.status_code == status.HTTP_307_TEMPORARY_REDIRECT
        and "Location" in exc.headers
    ):
        return RedirectResponse(url=exc.headers["Location"])
    return HTMLResponse(content=f"Error: {exc.detail}", status_code=exc.status_code)


@app.get("/health")
async def health_check():
    return {"status": "ok"}


def _extract_qr_transaction_id(body) -> str:
    """Pull the transaction id from either a flat or a `data`-wrapped body."""
    if not isinstance(body, dict):
        return ""
    candidate = body.get("qr_transaction_id")
    if not candidate and isinstance(body.get("data"), dict):
        candidate = body["data"].get("qr_transaction_id")
    # Unauthenticated public route: cap what we log/carry forward. Real xPay
    # transaction ids are ~25 characters, so 128 is generous.
    return str(candidate or "").strip()[:128]


@app.post(WEBHOOK_PATH)
async def xpay_webhook(request: Request):
    """Public, unauthenticated: xPay cannot present an admin session cookie.

    The callback is unsigned, so nothing in the body is trusted. We take
    only the transaction id and then ask the xPay status endpoint what
    actually happened. A forged request can at worst cost us one status
    call for a transaction that already exists.

    Always returns 200 - including on internal failure - so xPay stops
    retrying. The user's "Check payment" button remains the fallback.
    """
    # With the mock provider every status check answers COMPLETED, so an
    # unauthenticated caller who guessed a transaction id could settle a
    # payment. Nothing legitimate calls this route unless xPay is active.
    if is_mock():
        return {"status": "ok"}

    try:
        body = await request.json()
    except Exception:
        return {"status": "ok"}

    qr_transaction_id = _extract_qr_transaction_id(body)
    if not qr_transaction_id:
        return {"status": "ok"}

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Payment).where(
                    Payment.qr_transaction_id == qr_transaction_id
                )
            )
            payment = result.scalar_one_or_none()
            payment_id = payment.id if payment is not None else None

        if payment_id is None:
            logger.info(f"xPay webhook for unknown transaction {qr_transaction_id}")
            return {"status": "ok"}

        await confirm_payment(payment_id)
    except Exception as e:
        logger.warning(f"xPay webhook failed for {qr_transaction_id}: {e}")

    return {"status": "ok"}


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools_probe():
    return {}


@app.get("/")
async def root():
    return RedirectResponse(url="/admin")


# --- MEDIA PROXY ROUTE ---
@app.get("/admin/media/photo/{file_id}")
async def get_telegram_photo(file_id: str, _=Depends(require_admin)):
    bot = get_bot()
    try:
        tg_file = await bot.get_file(file_id)
        if tg_file and tg_file.file_path:
            url = f"https://api.telegram.org/file/bot{config.bot_token}/{tg_file.file_path}"
            return RedirectResponse(url=url)
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as e:
        logger.error(f"Error fetching telegram photo {file_id}: {e}")
        raise HTTPException(status_code=404, detail="Photo not found")


# --- AUTH ROUTES ---
PBKDF2_ITERATIONS = 260_000


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, PBKDF2_ITERATIONS
    ).hex()


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    if request.session.get("admin_logged_in"):
        return RedirectResponse(url="/admin")
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": error}
    )


@app.post("/admin/login")
async def login_action(
    request: Request, username: str = Form(...), password: str = Form(...)
):
    password_ok = False
    if username == config.admin_username:
        stored_hash = await get_setting("admin_password_hash")
        stored_salt = await get_setting("admin_password_salt")
        if stored_hash and stored_salt:
            # 260k-round PBKDF2 blocks the event loop for ~100-200ms; bot/main.py
            # runs polling and uvicorn on that same loop, so an unauthenticated
            # flood of login attempts would stall scheduled ad posts. Run it in
            # a worker thread.
            candidate = await asyncio.to_thread(
                _hash_password, password, bytes.fromhex(stored_salt)
            )
            password_ok = secrets.compare_digest(candidate, stored_hash)
        else:
            # No password has been saved through the panel yet -- fall back to
            # the plaintext value from .env so the current password keeps working.
            password_ok = secrets.compare_digest(password, config.admin_password)

    if password_ok:
        request.session["admin_logged_in"] = True
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "Неверный логин или пароль"},
        status_code=400,
    )


@app.get("/admin/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login")


# --- SHARED FILTER HELPERS ---
PERIOD_DAYS = {"today": 1, "7d": 7, "30d": 30, "90d": 90}


def _utc_now() -> datetime:
    """Naive UTC 'now', matching how created_at is stored on the models."""
    return datetime.now(UTC).replace(tzinfo=None)


def _period_start(period: str | None) -> datetime | None:
    """Naive-UTC start of a named period, or None for 'all'.

    "Today" means today in Bishkek, converted back to UTC for the comparison;
    the rolling windows are plain durations and need no conversion.
    """
    if not period or period == "all":
        return None
    if period == "today":
        local_midnight = datetime.now(LOCAL_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return local_midnight.astimezone(UTC).replace(tzinfo=None)
    days = PERIOD_DAYS.get(period)
    if not days:
        return None
    return _utc_now() - timedelta(days=days)


def _filter_qs(**params) -> str:
    """Build a querystring of the active filters (skipping defaults/empties)."""
    parts = []
    for key, value in params.items():
        if value in (None, "", "all"):
            continue
        parts.append(f"{key}={urllib.parse.quote_plus(str(value))}")
    return "&".join(parts)


# --- DASHBOARD ---
@app.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request, _=Depends(require_admin)):
    async with AsyncSessionLocal() as session:
        # Active campaigns
        act_res = await session.execute(
            select(func.count(Campaign.id)).where(
                Campaign.is_active == True, Campaign.publications_left > 0
            )
        )
        active_campaigns = act_res.scalar() or 0

        # Total published
        pub_res = await session.execute(
            select(func.sum(Campaign.publications_total - Campaign.publications_left))
        )
        total_published_count = pub_res.scalar() or 0

        # Total users
        usr_res = await session.execute(select(func.count(User.id)))
        total_users = usr_res.scalar() or 0

        # Recent campaigns
        rec_res = await session.execute(
            select(Campaign).order_by(desc(Campaign.id)).limit(10)
        )
        recent_campaigns = rec_res.scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "active_page": "dashboard",
            "active_campaigns": active_campaigns,
            "total_published_count": total_published_count,
            "total_users": total_users,
            "recent_campaigns": recent_campaigns,
        },
    )


# --- CAMPAIGNS (Search, Filters & Pagination) ---
@app.get("/admin/campaigns", response_class=HTMLResponse)
async def campaigns_page(
    request: Request,
    q: str | None = None,
    status_filter: str = Query("all", alias="status"),
    period: str = Query("all"),
    photo: str = Query("all"),
    sort: str = Query("new"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    msg: str | None = None,
    _=Depends(require_admin),
):
    filters = []

    if q and q.strip():
        term = q.strip()
        if term.isdigit():
            filters.append(
                or_(
                    Campaign.id == int(term),
                    Campaign.user_id == int(term),
                    Campaign.content_text.ilike(f"%{term}%"),
                )
            )
        else:
            filters.append(Campaign.content_text.ilike(f"%{term}%"))

    if status_filter == "active":
        filters.append(Campaign.is_active.is_(True))
        filters.append(Campaign.publications_left > 0)
    elif status_filter == "stopped":
        filters.append(Campaign.is_active.is_(False))
        filters.append(Campaign.publications_left > 0)
    elif status_filter == "completed":
        filters.append(Campaign.publications_left <= 0)

    since = _period_start(period)
    if since is not None:
        filters.append(Campaign.created_at >= since)

    if photo == "yes":
        filters.append(Campaign.content_photo.is_not(None))
        filters.append(Campaign.content_photo != "")
    elif photo == "no":
        filters.append(
            or_(Campaign.content_photo.is_(None), Campaign.content_photo == "")
        )

    order_by = {
        "new": desc(Campaign.id),
        "old": asc(Campaign.id),
        "left_desc": desc(Campaign.publications_left),
        "total_desc": desc(Campaign.publications_total),
    }.get(sort, desc(Campaign.id))

    async with AsyncSessionLocal() as session:
        base_query = select(Campaign)
        count_query = select(func.count(Campaign.id))
        for f in filters:
            base_query = base_query.where(f)
            count_query = count_query.where(f)

        tot_count_res = await session.execute(count_query)
        total_items = tot_count_res.scalar() or 0
        total_pages = max(1, math.ceil(total_items / limit))
        current_page = min(page, total_pages)

        res = await session.execute(
            base_query.order_by(order_by)
            .offset((current_page - 1) * limit)
            .limit(limit)
        )
        campaigns = res.scalars().all()

        # Summary of the whole filtered set (not just the current page)
        sum_query = select(func.coalesce(func.sum(Campaign.publications_left), 0))
        for f in filters:
            sum_query = sum_query.where(f)
        sum_res = await session.execute(sum_query)
        filtered_left = sum_res.scalar()

        # User metadata for the interactive popup (2 queries instead of N+1)
        user_ids = list({c.user_id for c in campaigns})
        user_map = {}
        if user_ids:
            stats_res = await session.execute(
                select(Campaign.user_id, func.count(Campaign.id))
                .where(Campaign.user_id.in_(user_ids))
                .group_by(Campaign.user_id)
            )
            stats = dict(stats_res.all())

            users_res = await session.execute(select(User).where(User.id.in_(user_ids)))
            for u in users_res.scalars().all():
                user_map[u.id] = {
                    "id": u.id,
                    "username": u.username or "",
                    "language": "🇰🇬 Кыргызча" if u.language == "ky" else "🇷🇺 Русский",
                    "is_banned": getattr(u, "is_banned", False),
                    "campaigns_count": stats.get(u.id, 0),
                }

    filter_qs = _filter_qs(
        q=q, status=status_filter, period=period, photo=photo, sort=sort,
        limit=limit if limit != 10 else None,
    )
    active_filters = sum(
        1
        for v in (status_filter, period, photo)
        if v not in (None, "", "all")
    ) + (1 if sort != "new" else 0)

    return templates.TemplateResponse(
        request=request,
        name="campaigns.html",
        context={
            "active_page": "campaigns",
            "campaigns": campaigns,
            "user_map": user_map,
            "q": q or "",
            "status": status_filter,
            "period": period,
            "photo": photo,
            "sort": sort,
            "limit": limit,
            "filter_qs": filter_qs,
            "active_filters": active_filters,
            "filtered_left": filtered_left or 0,
            "page": current_page,
            "total_pages": total_pages,
            "total_items": total_items,
            "msg": msg,
        },
    )


def _redirect_with_msg(redirect_to: str | None, fallback: str, msg: str):
    """Redirect back to the exact filtered view the action was fired from."""
    target = fallback
    if redirect_to and redirect_to.startswith("/admin"):
        target = redirect_to

    parsed = urllib.parse.urlsplit(target)
    params = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if k != "msg"
    ]
    params.append(("msg", msg))
    url = urllib.parse.urlunsplit(
        ("", "", parsed.path, urllib.parse.urlencode(params), "")
    )
    return RedirectResponse(url=url, status_code=302)


@app.post("/admin/campaigns/{campaign_id}/stop")
async def admin_stop_campaign(
    campaign_id: int,
    redirect_to: str | None = Form(None),
    _=Depends(require_admin),
):
    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign and campaign.is_active:
            campaign.is_active = False
            remove_campaign_job(campaign)
            await session.commit()
    return _redirect_with_msg(
        redirect_to, "/admin/campaigns", "Объявление успешно остановлено"
    )


@app.post("/admin/campaigns/{campaign_id}/resume")
async def admin_resume_campaign(
    campaign_id: int,
    redirect_to: str | None = Form(None),
    _=Depends(require_admin),
):
    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign and not campaign.is_active and campaign.publications_left > 0:
            campaign.is_active = True
            campaign.failure_count = 0
            campaign.job_id = schedule_campaign(campaign)
            await session.commit()
    return _redirect_with_msg(
        redirect_to, "/admin/campaigns", "Объявление возобновлено"
    )


@app.post("/admin/campaigns/{campaign_id}/delete")
async def admin_delete_campaign(
    campaign_id: int,
    delete_from_group: bool = Form(False),
    redirect_to: str | None = Form(None),
    _=Depends(require_admin),
):
    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign:
            if delete_from_group:
                bot = get_bot()
                try:
                    msg_id_list = []
                    if campaign.message_ids:
                        msg_id_list.extend(
                            [
                                int(x.strip())
                                for x in campaign.message_ids.split(",")
                                if x.strip().isdigit()
                            ]
                        )
                    if (
                        campaign.last_message_id
                        and campaign.last_message_id not in msg_id_list
                    ):
                        msg_id_list.append(campaign.last_message_id)

                    for m_id in set(msg_id_list):
                        try:
                            await bot.delete_message(
                                chat_id=config.group_id, message_id=m_id
                            )
                        except Exception as e:
                            logger.debug(
                                f"Failed to delete message {m_id} from group: {e}"
                            )
                except Exception as e:
                    logger.error(f"Error during group message deletion: {e}")

            remove_campaign_job(campaign)

            await session.delete(campaign)
            await session.commit()

    msg = (
        "Объявление удалено из базы и стерто из группы"
        if delete_from_group
        else "Объявление удалено из базы"
    )
    return _redirect_with_msg(redirect_to, "/admin/campaigns", msg)


# --- USERS (Search, Filters & Pagination) ---
@app.get("/admin/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    q: str | None = None,
    lang: str = Query("all"),
    status_filter: str = Query("all", alias="status"),
    activity: str = Query("all"),
    period: str = Query("all"),
    sort: str = Query("new"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    msg: str | None = None,
    _=Depends(require_admin),
):
    # Per-user campaign aggregates, computed in one grouped subquery
    agg = (
        select(
            Campaign.user_id.label("uid"),
            func.count(Campaign.id).label("c_count"),
        )
        .group_by(Campaign.user_id)
        .subquery()
    )
    c_count = func.coalesce(agg.c.c_count, 0)

    filters = []

    if q and q.strip():
        term = q.strip().lstrip("@")
        if term.isdigit():
            filters.append(
                or_(User.id == int(term), User.username.ilike(f"%{term}%"))
            )
        else:
            filters.append(User.username.ilike(f"%{term}%"))

    if lang in ("ru", "ky"):
        filters.append(User.language == lang)

    if status_filter == "banned":
        filters.append(User.is_banned.is_(True))
    elif status_filter == "active":
        filters.append(or_(User.is_banned.is_(False), User.is_banned.is_(None)))

    if activity == "customers":
        filters.append(c_count > 0)
    elif activity == "idle":
        filters.append(c_count == 0)

    since = _period_start(period)
    if since is not None:
        filters.append(User.created_at >= since)

    order_by = {
        "new": desc(User.id),
        "old": asc(User.id),
        "campaigns_desc": desc(c_count),
        # Nameless accounts sort last instead of leading the list
        "username": asc(func.lower(func.coalesce(User.username, "яяяя"))),
    }.get(sort, desc(User.id))

    async with AsyncSessionLocal() as session:
        base_query = select(User, c_count.label("c_count")).outerjoin(
            agg, agg.c.uid == User.id
        )
        count_query = (
            select(func.count()).select_from(User).outerjoin(agg, agg.c.uid == User.id)
        )
        for f in filters:
            base_query = base_query.where(f)
            count_query = count_query.where(f)

        tot_count_res = await session.execute(count_query)
        total_items = tot_count_res.scalar() or 0
        total_pages = max(1, math.ceil(total_items / limit))
        current_page = min(page, total_pages)

        rows_res = await session.execute(
            base_query.order_by(order_by)
            .offset((current_page - 1) * limit)
            .limit(limit)
        )

        users = []
        for u, user_campaigns in rows_res.all():
            users.append(
                {
                    "id": u.id,
                    "username": u.username or "",
                    "language": u.language,
                    "is_banned": getattr(u, "is_banned", False),
                    "campaigns_count": user_campaigns or 0,
                    "created_at": u.created_at,
                }
            )

        # Totals across the whole filtered set
        sum_query = (
            select(func.coalesce(func.sum(c_count), 0))
            .select_from(User)
            .outerjoin(agg, agg.c.uid == User.id)
        )
        for f in filters:
            sum_query = sum_query.where(f)
        sum_res = await session.execute(sum_query)
        filtered_campaigns = sum_res.scalar()

    filter_qs = _filter_qs(
        q=q, lang=lang, status=status_filter, activity=activity, period=period,
        sort=sort, limit=limit if limit != 10 else None,
    )
    active_filters = sum(
        1 for v in (lang, status_filter, activity, period) if v not in (None, "", "all")
    ) + (1 if sort != "new" else 0)

    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "active_page": "users",
            "users": users,
            "q": q or "",
            "lang": lang,
            "status": status_filter,
            "activity": activity,
            "period": period,
            "sort": sort,
            "limit": limit,
            "filter_qs": filter_qs,
            "active_filters": active_filters,
            "filtered_campaigns": filtered_campaigns or 0,
            "page": current_page,
            "total_pages": total_pages,
            "total_items": total_items,
            "msg": msg,
        },
    )


@app.post("/admin/users/{user_id}/toggle-ban")
async def toggle_user_ban(
    user_id: int, redirect_to: str | None = None, _=Depends(require_admin)
):
    status_text = "обновлен"
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if user:
            user.is_banned = not getattr(user, "is_banned", False)
            status_text = (
                "заблокирован в боте" if user.is_banned else "разблокирован в боте"
            )

            if user.is_banned:
                c_res = await session.execute(
                    select(Campaign).where(
                        Campaign.user_id == user_id, Campaign.is_active == True
                    )
                )
                for c in c_res.scalars().all():
                    c.is_active = False
                    remove_campaign_job(c)

            await session.commit()

    return _redirect_with_msg(
        redirect_to, "/admin/users", f"Пользователь {user_id} {status_text}"
    )


@app.post("/admin/users/{user_id}/kick-from-group")
async def kick_user_from_group(
    user_id: int, redirect_to: str | None = None, _=Depends(require_admin)
):
    group_msg = ""
    if not config.group_id:
        group_msg = "ID группы не настроен"
    else:
        bot = get_bot()
        try:
            # Kick from group (ban then unban so they are removed from group)
            await bot.ban_chat_member(chat_id=config.group_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=config.group_id, user_id=user_id)
            group_msg = "исключен из Telegram-группы"
        except Exception as e:
            logger.error(f"Failed to kick user {user_id} from group: {e}")
            group_msg = f"ошибка группы ({str(e)[:30]})"

    # Now remove all campaigns & jobs and delete user from database list
    async with AsyncSessionLocal() as session:
        # Stop any running scheduler jobs
        c_res = await session.execute(
            select(Campaign).where(Campaign.user_id == user_id)
        )
        for c in c_res.scalars().all():
            remove_campaign_job(c)

        # Delete user's campaigns
        await session.execute(delete(Campaign).where(Campaign.user_id == user_id))

        # Delete user from users table
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()

    msg = f"Пользователь {user_id} {group_msg} и удален из базы пользователей"
    return _redirect_with_msg(redirect_to, "/admin/users", msg)


@app.post("/admin/users/{user_id}/delete")
async def delete_user_from_db(
    user_id: int, redirect_to: str | None = None, _=Depends(require_admin)
):
    async with AsyncSessionLocal() as session:
        c_res = await session.execute(
            select(Campaign).where(Campaign.user_id == user_id)
        )
        for c in c_res.scalars().all():
            remove_campaign_job(c)
        await session.execute(delete(Campaign).where(Campaign.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()

    msg = f"Пользователь {user_id} удален из базы данных"
    return _redirect_with_msg(redirect_to, "/admin/users", msg)


# --- BROADCAST & PROMO ---
GROUP_POST_ALLOWED_INTERVALS = {5, 15, 30, 60, 180, 720, 1440}
MAX_GROUP_POST_REPEATS = 100


@app.get("/admin/broadcast", response_class=HTMLResponse)
async def broadcast_page(
    request: Request, msg: str | None = None, _=Depends(require_admin)
):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(func.count(User.id)))
        total_users = res.scalar() or 0

        gp_res = await session.execute(
            select(GroupPost)
            .where(GroupPost.is_active.is_(True))
            .order_by(desc(GroupPost.id))
        )
        active_group_posts = gp_res.scalars().all()

    bot_username = ""
    bot = get_bot()
    try:
        me = await bot.get_me()
        bot_username = me.username or ""
    except Exception:
        pass

    return templates.TemplateResponse(
        request=request,
        name="broadcast.html",
        context={
            "active_page": "broadcast",
            "total_users": total_users,
            "bot_username": bot_username,
            "active_group_posts": active_group_posts,
            "msg": msg,
        },
    )


@app.post("/admin/broadcast/group-post")
async def broadcast_custom_group_post(request: Request, _=Depends(require_admin)):
    form_data = await request.form()
    text = form_data.get("text", "").strip()
    btn_texts = form_data.getlist("btn_text")
    btn_urls = form_data.getlist("btn_url")

    if not text:
        return RedirectResponse(
            url="/admin/broadcast?msg=Текст+сообщения+не+может+быть+пустым",
            status_code=302,
        )

    try:
        repeats_total = int(form_data.get("repeats_total", "1"))
    except ValueError:
        repeats_total = 1
    try:
        interval_minutes = int(form_data.get("interval_minutes", "60"))
    except ValueError:
        interval_minutes = 60

    if not (1 <= repeats_total <= MAX_GROUP_POST_REPEATS):
        return RedirectResponse(
            url="/admin/broadcast?msg=Некорректное+количество+повторов",
            status_code=302,
        )
    if repeats_total > 1 and interval_minutes not in GROUP_POST_ALLOWED_INTERVALS:
        return RedirectResponse(
            url="/admin/broadcast?msg=Некорректный+интервал+повторов",
            status_code=302,
        )

    buttons = []
    for t, u in zip(btn_texts, btn_urls):
        t_clean = str(t).strip()
        u_clean = str(u).strip()
        if t_clean and u_clean:
            buttons.append({"text": t_clean, "url": u_clean})
            if len(buttons) >= 5:
                break

    pin_message = form_data.get("pin_message") == "on"

    markup = build_group_post_markup(json.dumps(buttons))

    bot = get_bot()
    try:
        sent_msg = await bot.send_message(
            chat_id=config.group_id, text=text, reply_markup=markup
        )
    except Exception as e:
        logger.error(f"Failed to post custom message to group: {e}")
        msg = f"Ошибка+отправки:+{e}"
        return RedirectResponse(url=f"/admin/broadcast?msg={msg}", status_code=302)

    if pin_message:
        try:
            await bot.pin_chat_message(
                chat_id=config.group_id, message_id=sent_msg.message_id
            )
        except Exception as e:
            # Pinning is a nice-to-have on top of a post that already sent
            # successfully: a permissions error here shouldn't look like the
            # whole publish failed.
            logger.error(f"Failed to pin group post message: {e}")

    repeats_left = repeats_total - 1
    async with AsyncSessionLocal() as session:
        post = GroupPost(
            text=text,
            buttons=json.dumps(buttons),
            repeats_total=repeats_total,
            repeats_left=repeats_left,
            interval_minutes=interval_minutes,
            is_active=repeats_left > 0,
        )
        session.add(post)
        await session.commit()

        if repeats_left > 0:
            post.job_id = schedule_group_post(post)
            await session.commit()

    if repeats_left > 0:
        msg = (
            f"Пост+с+{len(buttons)}+кнопками+опубликован!+"
            f"Запланировано+еще+{repeats_left}+повтор(ов)+каждые+{interval_minutes}+мин."
        )
    else:
        msg = f"Пост+с+{len(buttons)}+кнопками+успешно+опубликован+в+группу!"

    return RedirectResponse(url=f"/admin/broadcast?msg={msg}", status_code=302)


@app.post("/admin/broadcast/group-post/{post_id}/stop")
async def stop_group_post(post_id: int, _=Depends(require_admin)):
    async with AsyncSessionLocal() as session:
        post = await session.get(GroupPost, post_id)
        if post and post.is_active:
            post.is_active = False
            remove_group_post_job(post)
            await session.commit()

    return RedirectResponse(
        url="/admin/broadcast?msg=Повторяющийся+пост+остановлен", status_code=302
    )


@app.post("/admin/broadcast/users")
async def broadcast_users(
    text: str = Form(...), target_lang: str = Form("all"), _=Depends(require_admin)
):
    async def send_all():
        bot = get_bot()
        async with AsyncSessionLocal() as session:
            query = select(User)
            if target_lang != "all":
                query = query.where(User.language == target_lang)
            res = await session.execute(query)
            users = res.scalars().all()

        sent_count = 0
        for u in users:
            try:
                await bot.send_message(chat_id=u.id, text=text)
                sent_count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.debug(f"Broadcast failed for user {u.id}: {e}")

        logger.info(f"Broadcast finished. Sent to {sent_count} users.")

    _spawn(send_all())
    return RedirectResponse(
        url="/admin/broadcast?msg=Рассылка+запущена+в+фоновом+режиме!", status_code=302
    )


# --- SETTINGS ---
@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    _=Depends(require_admin),
):
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active_page": "settings",
            "banned_words_count": len(BANNED_WORDS),
            "msg": msg,
            "err": err,
        },
    )


@app.post("/admin/settings/general")
async def settings_general_action(
    new_password: str | None = Form(None),
    _=Depends(require_admin),
):
    if new_password and new_password.strip():
        salt = secrets.token_bytes(16)
        password_hash = await asyncio.to_thread(
            _hash_password, new_password.strip(), salt
        )
        # Both keys in one transaction: a partial write (new salt, old hash)
        # would make every future login fail with no recovery path.
        await set_settings(
            {"admin_password_salt": salt.hex(), "admin_password_hash": password_hash}
        )

    return RedirectResponse(
        url="/admin/settings?msg=Настройки+успешно+сохранены!", status_code=302
    )
