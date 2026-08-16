import asyncio
import logging
import math
import os
import urllib.parse

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, desc, func, or_, select
from starlette.middleware.sessions import SessionMiddleware

from bot.config import config
from bot.database.db import AsyncSessionLocal
from bot.database.models import Campaign, User
from bot.handlers.ad_flow import post_ad
from bot.services.moderation import BANNED_WORDS
from bot.services.scheduler import scheduler

logger = logging.getLogger(__name__)

app = FastAPI(title="Poputka Admin Panel", docs_url=None, redoc_url=None)

# Session middleware for cookie-based authentication
app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key,
    max_age=3600 * 24 * 7,  # 7 days
)

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)


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


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools_probe():
    return {}


@app.get("/")
async def root():
    return RedirectResponse(url="/admin")


# --- MEDIA PROXY ROUTE ---
@app.get("/admin/media/photo/{file_id}")
async def get_telegram_photo(file_id: str, _=Depends(require_admin)):
    bot = Bot(token=config.bot_token)
    try:
        tg_file = await bot.get_file(file_id)
        if tg_file and tg_file.file_path:
            url = f"https://api.telegram.org/file/bot{config.bot_token}/{tg_file.file_path}"
            return RedirectResponse(url=url)
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as e:
        logger.error(f"Error fetching telegram photo {file_id}: {e}")
        raise HTTPException(status_code=404, detail="Photo not found")
    finally:
        await bot.session.close()


# --- AUTH ROUTES ---
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
    if username == config.admin_username and password == config.admin_password:
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


# --- DASHBOARD ---
@app.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request, _=Depends(require_admin)):
    async with AsyncSessionLocal() as session:
        # Total revenue
        rev_res = await session.execute(select(func.sum(Campaign.price_paid)))
        total_revenue = rev_res.scalar() or 0.0

        # If price_paid wasn't tracked for older rows, fallback to total * 1.0
        if total_revenue == 0.0:
            tot_res = await session.execute(
                select(func.sum(Campaign.publications_total))
            )
            total_revenue = float(tot_res.scalar() or 0) * 1.0

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
            "total_revenue": round(total_revenue, 2),
            "active_campaigns": active_campaigns,
            "total_published_count": total_published_count,
            "total_users": total_users,
            "recent_campaigns": recent_campaigns,
        },
    )


# --- CAMPAIGNS (With Search & Pagination) ---
@app.get("/admin/campaigns", response_class=HTMLResponse)
async def campaigns_page(
    request: Request,
    q: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    msg: str | None = None,
    _=Depends(require_admin),
):
    async with AsyncSessionLocal() as session:
        base_query = select(Campaign)
        count_query = select(func.count(Campaign.id))

        if q and q.strip():
            term = q.strip()
            if term.isdigit():
                filt = or_(
                    Campaign.id == int(term),
                    Campaign.user_id == int(term),
                    Campaign.content_text.ilike(f"%{term}%"),
                )
            else:
                filt = Campaign.content_text.ilike(f"%{term}%")
            base_query = base_query.where(filt)
            count_query = count_query.where(filt)

        # Count total matching items
        tot_count_res = await session.execute(count_query)
        total_items = tot_count_res.scalar() or 0
        total_pages = max(1, math.ceil(total_items / limit))
        current_page = min(page, total_pages)

        # Paginated items
        res = await session.execute(
            base_query.order_by(desc(Campaign.id))
            .offset((current_page - 1) * limit)
            .limit(limit)
        )
        campaigns = res.scalars().all()

        # User metadata map for interactive popup
        user_ids = list({c.user_id for c in campaigns})
        user_map = {}
        if user_ids:
            users_res = await session.execute(select(User).where(User.id.in_(user_ids)))
            for u in users_res.scalars().all():
                c_res = await session.execute(
                    select(Campaign).where(Campaign.user_id == u.id)
                )
                c_list = c_res.scalars().all()
                spent = sum(
                    c.price_paid or (c.publications_total * 1.0) for c in c_list
                )
                user_map[u.id] = {
                    "id": u.id,
                    "username": u.username or "",
                    "language": "🇰🇬 Кыргызча" if u.language == "ky" else "🇷🇺 Русский",
                    "is_banned": getattr(u, "is_banned", False),
                    "campaigns_count": len(c_list),
                    "total_spent": round(spent, 2),
                }

    return templates.TemplateResponse(
        request=request,
        name="campaigns.html",
        context={
            "active_page": "campaigns",
            "campaigns": campaigns,
            "user_map": user_map,
            "q": q or "",
            "page": current_page,
            "total_pages": total_pages,
            "total_items": total_items,
            "msg": msg,
        },
    )


@app.post("/admin/campaigns/{campaign_id}/stop")
async def admin_stop_campaign(campaign_id: int, _=Depends(require_admin)):
    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign and campaign.is_active:
            campaign.is_active = False
            if campaign.job_id:
                try:
                    scheduler.remove_job(campaign.job_id)
                except Exception:
                    pass
            await session.commit()
    return RedirectResponse(
        url="/admin/campaigns?msg=Объявление+успешно+остановлено", status_code=302
    )


@app.post("/admin/campaigns/{campaign_id}/resume")
async def admin_resume_campaign(campaign_id: int, _=Depends(require_admin)):
    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign and not campaign.is_active and campaign.publications_left > 0:
            campaign.is_active = True
            job = scheduler.add_job(
                post_ad,
                "interval",
                minutes=campaign.interval_minutes,
                args=[campaign.user_id, campaign.id],
            )
            campaign.job_id = job.id
            await session.commit()
    return RedirectResponse(
        url="/admin/campaigns?msg=Объявление+возобновлено", status_code=302
    )


@app.post("/admin/campaigns/{campaign_id}/delete")
async def admin_delete_campaign(
    campaign_id: int, delete_from_group: bool = Form(False), _=Depends(require_admin)
):
    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign:
            if delete_from_group:
                bot = Bot(token=config.bot_token)
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
                finally:
                    await bot.session.close()

            if campaign.job_id:
                try:
                    scheduler.remove_job(campaign.job_id)
                except Exception:
                    pass

            await session.delete(campaign)
            await session.commit()

    msg = (
        "Объявление+удалено+из+базы+и+стерто+из+группы"
        if delete_from_group
        else "Объявление+удалено+из+базы"
    )
    return RedirectResponse(url=f"/admin/campaigns?msg={msg}", status_code=302)


# --- USERS (With Search & Pagination) ---
@app.get("/admin/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    q: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    msg: str | None = None,
    _=Depends(require_admin),
):
    async with AsyncSessionLocal() as session:
        base_query = select(User)
        count_query = select(func.count(User.id))

        if q and q.strip():
            term = q.strip().lstrip("@")
            if term.isdigit():
                filt = or_(User.id == int(term), User.username.ilike(f"%{term}%"))
            else:
                filt = or_(
                    User.username.ilike(f"%{term}%"), User.language.ilike(f"%{term}%")
                )
            base_query = base_query.where(filt)
            count_query = count_query.where(filt)

        tot_count_res = await session.execute(count_query)
        total_items = tot_count_res.scalar() or 0
        total_pages = max(1, math.ceil(total_items / limit))
        current_page = min(page, total_pages)

        usr_res = await session.execute(
            base_query.order_by(desc(User.id))
            .offset((current_page - 1) * limit)
            .limit(limit)
        )
        users_raw = usr_res.scalars().all()

        users = []
        for u in users_raw:
            c_res = await session.execute(
                select(Campaign).where(Campaign.user_id == u.id)
            )
            c_list = c_res.scalars().all()
            spent = sum(c.price_paid or (c.publications_total * 1.0) for c in c_list)

            users.append(
                {
                    "id": u.id,
                    "username": u.username or "",
                    "language": u.language,
                    "is_banned": getattr(u, "is_banned", False),
                    "campaigns_count": len(c_list),
                    "total_spent": round(spent, 2),
                }
            )

    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "active_page": "users",
            "users": users,
            "q": q or "",
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
                    if c.job_id:
                        try:
                            scheduler.remove_job(c.job_id)
                        except Exception:
                            pass

            await session.commit()

    target_url = redirect_to if redirect_to else "/admin/users"
    sep = "&" if "?" in target_url else "?"
    msg = f"Пользователь {user_id} {status_text}"
    return RedirectResponse(
        url=f"{target_url}{sep}msg={urllib.parse.quote_plus(msg)}", status_code=302
    )


@app.post("/admin/users/{user_id}/kick-from-group")
async def kick_user_from_group(
    user_id: int, redirect_to: str | None = None, _=Depends(require_admin)
):
    group_msg = ""
    if not config.group_id:
        group_msg = "ID группы не настроен"
    else:
        bot = Bot(token=config.bot_token)
        try:
            # Kick from group (ban then unban so they are removed from group)
            await bot.ban_chat_member(chat_id=config.group_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=config.group_id, user_id=user_id)
            group_msg = "исключен из Telegram-группы"
        except Exception as e:
            logger.error(f"Failed to kick user {user_id} from group: {e}")
            group_msg = f"ошибка группы ({str(e)[:30]})"
        finally:
            await bot.session.close()

    # Now remove all campaigns & jobs and delete user from database list
    async with AsyncSessionLocal() as session:
        # Stop any running scheduler jobs
        c_res = await session.execute(
            select(Campaign).where(Campaign.user_id == user_id)
        )
        for c in c_res.scalars().all():
            if c.job_id:
                try:
                    scheduler.remove_job(c.job_id)
                except Exception:
                    pass

        # Delete user's campaigns
        await session.execute(delete(Campaign).where(Campaign.user_id == user_id))

        # Delete user from users table
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()

    msg = f"Пользователь {user_id} {group_msg} и удален из базы пользователей"
    target_url = redirect_to if redirect_to else "/admin/users"
    sep = "&" if "?" in target_url else "?"
    return RedirectResponse(
        url=f"{target_url}{sep}msg={urllib.parse.quote_plus(msg)}", status_code=302
    )


@app.post("/admin/users/{user_id}/delete")
async def delete_user_from_db(
    user_id: int, redirect_to: str | None = None, _=Depends(require_admin)
):
    async with AsyncSessionLocal() as session:
        c_res = await session.execute(
            select(Campaign).where(Campaign.user_id == user_id)
        )
        for c in c_res.scalars().all():
            if c.job_id:
                try:
                    scheduler.remove_job(c.job_id)
                except Exception:
                    pass
        await session.execute(delete(Campaign).where(Campaign.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()

    msg = f"Пользователь {user_id} удален из базы данных"
    target_url = redirect_to if redirect_to else "/admin/users"
    sep = "&" if "?" in target_url else "?"
    return RedirectResponse(
        url=f"{target_url}{sep}msg={urllib.parse.quote_plus(msg)}", status_code=302
    )


# --- BROADCAST & PROMO ---
@app.get("/admin/broadcast", response_class=HTMLResponse)
async def broadcast_page(
    request: Request, msg: str | None = None, _=Depends(require_admin)
):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(func.count(User.id)))
        total_users = res.scalar() or 0

    bot_username = ""
    bot = Bot(token=config.bot_token)
    try:
        me = await bot.get_me()
        bot_username = me.username or ""
    except Exception:
        pass
    finally:
        await bot.session.close()

    return templates.TemplateResponse(
        request=request,
        name="broadcast.html",
        context={
            "active_page": "broadcast",
            "total_users": total_users,
            "bot_username": bot_username,
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

    buttons = []
    for t, u in zip(btn_texts, btn_urls):
        t_clean = str(t).strip()
        u_clean = str(u).strip()
        if t_clean and u_clean:
            buttons.append([InlineKeyboardButton(text=t_clean, url=u_clean)])
            if len(buttons) >= 5:
                break

    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    bot = Bot(token=config.bot_token)
    try:
        await bot.send_message(chat_id=config.group_id, text=text, reply_markup=markup)
        msg = f"Пост+с+{len(buttons)}+кнопками+успешно+опубликован+в+группу!"
    except Exception as e:
        logger.error(f"Failed to post custom message to group: {e}")
        msg = f"Ошибка+отправки:+{e}"
    finally:
        await bot.session.close()

    return RedirectResponse(url=f"/admin/broadcast?msg={msg}", status_code=302)


@app.post("/admin/broadcast/users")
async def broadcast_users(
    text: str = Form(...), target_lang: str = Form("all"), _=Depends(require_admin)
):
    async def send_all():
        bot = Bot(token=config.bot_token)
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

        await bot.session.close()
        logger.info(f"Broadcast finished. Sent to {sent_count} users.")

    asyncio.create_task(send_all())
    return RedirectResponse(
        url="/admin/broadcast?msg=Рассылка+запущена+в+фоновом+режиме!", status_code=302
    )


# --- SETTINGS ---
@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, msg: str | None = None, _=Depends(require_admin)
):
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active_page": "settings",
            "price_per_ad": 1.0,
            "banned_words_count": len(BANNED_WORDS),
            "msg": msg,
        },
    )


@app.post("/admin/settings/general")
async def settings_general_action(
    price_per_ad: float = Form(...),
    new_password: str | None = Form(None),
    _=Depends(require_admin),
):
    if new_password and new_password.strip():
        config.admin_password = new_password.strip()

    return RedirectResponse(
        url="/admin/settings?msg=Настройки+успешно+сохранены!", status_code=302
    )
