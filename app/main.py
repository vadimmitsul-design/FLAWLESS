import asyncio
import base64
import contextlib
import csv
import hashlib
import io
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import litellm
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app import billing, dlp, llm, pricing, ratelimit, reaper, telegram_bot
from app.csrf import CSRFOriginMiddleware
from app.config import session_secret_is_weak, settings
from app.db import get_session
from app.models import (
    ApiKey,
    Customer,
    DialogueArchive,
    PasswordResetRequest,
    Product,
    InviteCode,
    Prompt,
    SubscriptionOrder,
    TelegramLink,
    TelegramLinkCode,
    TopupRequest,
    UsageEvent,
    WalletLedger,
    WebConversation,
    WebMessage,
    utcnow,
)
from app.schemas import ChatCompletionRequest
from app.security import (
    generate_api_key,
    generate_temp_password,
    get_current_customer,
    get_customer_by_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_SIGNUP_MODES = {"invite", "open", "closed"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if session_secret_is_weak(settings.session_secret):
        # Слабый секрет = любая сессия подделывается (кука подписана, но не
        # зашифрована). В проде падаем на старте, в разработке громко ругаемся:
        # молча работать с дырявыми куками нельзя ни в одном режиме.
        # Раньше проверка сравнивала только с "change-me", а в .env.example
        # лежало "change-me-session-secret" — и не срабатывала никогда.
        if settings.environment == "production":
            raise RuntimeError(
                "SESSION_SECRET is default/weak — sessions would be forgeable. "
                "Set a random value of at least 32 characters (python -c \"import secrets; "
                "print(secrets.token_urlsafe(48))\") before running with ENVIRONMENT=production"
            )
        logger.warning(
            "SESSION_SECRET is default/weak — session cookies are forgeable. "
            "Acceptable locally, MUST be replaced before deploying."
        )
    if settings.signup_mode not in _SIGNUP_MODES:
        raise RuntimeError(f"SIGNUP_MODE must be one of {sorted(_SIGNUP_MODES)}, got '{settings.signup_mode}'")
    llm.init_router()
    telegram_task = None
    if settings.telegram_bot_token:
        telegram_task = asyncio.create_task(telegram_bot.poll_loop())
        logger.info("telegram bot polling task started")
    reaper_task = asyncio.create_task(reaper.reaper_loop())
    logger.info("stale-pending-event reaper task started")
    yield
    # cancel() без await — задача помечена отменённой, но её собственный
    # SessionLocal()/aiosqlite-хендл может не успеть закрыться до того, как
    # процесс/цикл событий уйдёт дальше (в тестах — до следующего TestClient).
    # На много итераций (полный прогон тестов — под сотню TestClient) это
    # накапливалось в утечку хендлов/потоков anyio-портала и давало
    # неустойчивые зависания в никак не связанных тестах. Дожидаемся отмены
    # явно.
    if telegram_task is not None:
        telegram_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await telegram_task
    reaper_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reaper_task


app = FastAPI(title="Flawless", lifespan=lifespan)
app.add_middleware(CSRFOriginMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.environment == "production",
)


def _require_admin(customer: Customer | None) -> RedirectResponse | None:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    if customer.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return None


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ---------- веб: регистрация / логин ----------


@app.get("/signup")
async def signup_form(request: Request):
    if settings.signup_mode == "closed":
        return templates.TemplateResponse(request, "signup_closed.html", {}, status_code=403)
    return templates.TemplateResponse(
        request, "signup.html", {"error": None, "invite_required": settings.signup_mode == "invite"}
    )


@app.post("/signup")
async def signup_submit(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    invite: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    if settings.signup_mode == "closed":
        return templates.TemplateResponse(request, "signup_closed.html", {}, status_code=403)
    invite_required = settings.signup_mode == "invite"

    def _fail(message: str, status_code: int):
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"error": message, "invite_required": invite_required},
            status_code=status_code,
        )

    invite_code = None
    if invite_required:
        code = invite.strip()
        if not code:
            return _fail("Нужен код приглашения — запросите его у администратора", 400)
        invite_code = (
            await session.execute(
                select(InviteCode).where(InviteCode.code == code, InviteCode.used_by_customer_id.is_(None))
            )
        ).scalar_one_or_none()
        if invite_code is None:
            return _fail("Код приглашения не найден или уже использован", 400)

    email = email.strip().lower()
    exists = (
        await session.execute(select(Customer).where(Customer.email == email))
    ).scalar_one_or_none()
    if exists is not None:
        return _fail("Этот email уже зарегистрирован", 409)

    customer = Customer(email=email, name=name.strip(), password_hash=hash_password(password))
    session.add(customer)
    await session.flush()

    if invite_code is not None:
        # Гасим код атомарно: между SELECT выше и этим UPDATE тем же кодом мог
        # успеть зарегистрироваться кто-то ещё. rowcount==0 значит проиграли
        # гонку — не коммитим (сессия закроется и откатит вставку клиента),
        # rollback() руками не зовём: он экспайрит объекты сессии, см. billing.py.
        used = await session.execute(
            update(InviteCode)
            .where(InviteCode.id == invite_code.id, InviteCode.used_by_customer_id.is_(None))
            .values(used_by_customer_id=customer.id, used_at=utcnow())
        )
        if used.rowcount == 0:
            return _fail("Код приглашения только что использовали — запросите новый", 409)

    await session.commit()
    request.session["customer_id"] = customer.id
    return RedirectResponse("/", status_code=303)


@app.get("/login")
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    client_ip = request.client.host if request.client else "unknown"
    if not ratelimit.check_login(client_ip):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Слишком много попыток входа, попробуйте позже"}, status_code=429
        )
    customer = (
        await session.execute(
            select(Customer).where(Customer.email == email.strip().lower(), Customer.active)
        )
    ).scalar_one_or_none()
    if customer is None or not verify_password(password, customer.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный email или пароль"}, status_code=401
        )
    request.session["customer_id"] = customer.id
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/forgot-password")
async def forgot_password_form(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html", {"sent": False})


@app.post("/forgot-password")
async def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    customer = (
        await session.execute(
            select(Customer).where(Customer.email == email.strip().lower(), Customer.active)
        )
    ).scalar_one_or_none()
    if customer is not None:
        session.add(PasswordResetRequest(customer_id=customer.id))
        await session.commit()
    # Один и тот же ответ независимо от того, найден email или нет —
    # иначе форма превращается в способ проверить, кто зарегистрирован.
    return templates.TemplateResponse(request, "forgot_password.html", {"sent": True})


# ---------- веб: кабинет ----------


@app.get("/")
async def dashboard(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return templates.TemplateResponse(request, "landing.html", {"customer": None})

    api_keys = (
        await session.execute(
            select(ApiKey)
            .where(ApiKey.customer_id == customer.id, ApiKey.active)
            .order_by(ApiKey.created_at.desc())
        )
    ).scalars().all()
    events = (
        await session.execute(
            select(UsageEvent)
            .where(UsageEvent.customer_id == customer.id)
            .order_by(UsageEvent.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    my_topups = (
        await session.execute(
            select(TopupRequest)
            .where(TopupRequest.customer_id == customer.id)
            .order_by(TopupRequest.created_at.desc())
            .limit(10)
        )
    ).scalars().all()
    children = (
        await session.execute(
            select(Customer).where(Customer.parent_customer_id == customer.id).order_by(Customer.created_at)
        )
    ).scalars().all() if not customer.is_child else []
    telegram_link = (
        await session.execute(select(TelegramLink).where(TelegramLink.customer_id == customer.id))
    ).scalar_one_or_none()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "customer": customer,
            "api_keys": api_keys,
            "events": events,
            "topups": my_topups,
            "models": llm.known_models(),
            "children": children,
            "telegram_enabled": bool(settings.telegram_bot_token),
            "telegram_linked": telegram_link is not None,
            "telegram_bot_username": telegram_bot.bot_username,
            "telegram_code": request.query_params.get("telegram_code"),
        },
    )


@app.post("/api-key/regenerate")
async def create_api_key(
    request: Request,
    name: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    """Название маршрута сохранено для обратной совместимости (документация/
    закладки), поведение — уже не "перевыпуск", а "ещё один именованный
    ключ" (2.1 доработок): старые ключи больше не деактивируются."""
    if customer is None:
        return RedirectResponse("/login", status_code=303)

    raw_key = generate_api_key()
    session.add(
        ApiKey(
            customer_id=customer.id,
            name=name.strip() or "Без названия",
            key_hash=hash_api_key(raw_key),
            last_four=raw_key[-4:],
        )
    )
    await session.commit()
    return templates.TemplateResponse(
        request, "api_key_shown.html", {"customer": customer, "raw_key": raw_key}
    )


@app.post("/api-keys/{key_id}/revoke")
async def revoke_api_key(
    key_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    api_key = await session.get(ApiKey, key_id)
    if api_key is None or api_key.customer_id != customer.id:
        raise HTTPException(status_code=404)
    api_key.active = False
    await session.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/api-keys/{key_id}/limits")
async def set_api_key_limits(
    key_id: int,
    daily_limit_rub: str = Form(""),
    monthly_limit_rub: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    """Клиент настраивает СВОИ лимиты (2.2 доработок) — пусто = лимита нет.
    Не может снять/обойти admin_*_limit_rub — тот проверяется отдельно как
    потолок поверх (см. billing.check_api_key_spend_limits)."""
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    api_key = await session.get(ApiKey, key_id)
    if api_key is None or api_key.customer_id != customer.id:
        raise HTTPException(status_code=404)
    api_key.daily_limit_rub = Decimal(daily_limit_rub) if daily_limit_rub.strip() else None
    api_key.monthly_limit_rub = Decimal(monthly_limit_rub) if monthly_limit_rub.strip() else None
    await session.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/topups/new")
async def new_topup(
    amount_rub: Decimal = Form(...),
    note: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    if amount_rub <= 0:
        raise HTTPException(status_code=400, detail="amount_rub must be positive")
    session.add(TopupRequest(customer_id=customer.id, amount_rub=amount_rub, note=note.strip() or None))
    await session.commit()
    return RedirectResponse("/", status_code=303)


# ---------- веб: магазин подписок (платёжный агент) ----------


@app.get("/shop")
async def shop(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    products = (
        await session.execute(select(Product).where(Product.active).order_by(Product.price_rub))
    ).scalars().all()
    my_orders = (
        await session.execute(
            select(SubscriptionOrder, Product)
            .join(Product, Product.id == SubscriptionOrder.product_id)
            .where(SubscriptionOrder.customer_id == customer.id)
            .order_by(SubscriptionOrder.created_at.desc())
        )
    ).all()
    return templates.TemplateResponse(
        request, "shop.html", {"customer": customer, "products": products, "orders": my_orders, "error": None}
    )


@app.post("/shop/order")
async def shop_order(
    request: Request,
    product_id: int = Form(...),
    account_email: str = Form(...),
    note: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    product = await session.get(Product, product_id)
    if product is None or not product.active:
        raise HTTPException(status_code=404)
    try:
        await billing.purchase_subscription(
            session, customer.id, product, account_email.strip(), note.strip() or None
        )
    except billing.InsufficientBalance as e:
        products = (
            await session.execute(select(Product).where(Product.active).order_by(Product.price_rub))
        ).scalars().all()
        my_orders = (
            await session.execute(
                select(SubscriptionOrder, Product)
                .join(Product, Product.id == SubscriptionOrder.product_id)
                .where(SubscriptionOrder.customer_id == customer.id)
                .order_by(SubscriptionOrder.created_at.desc())
            )
        ).all()
        return templates.TemplateResponse(
            request,
            "shop.html",
            {
                "customer": customer,
                "products": products,
                "orders": my_orders,
                "error": f"Недостаточно средств: на балансе {e.balance} ₽, нужно {e.required} ₽. Пополните баланс в кабинете.",
            },
            status_code=402,
        )
    return RedirectResponse("/shop", status_code=303)


# ---------- веб: библиотека промптов ----------


@app.get("/prompts")
async def prompts_list(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    marketplace = (
        await session.execute(
            select(Prompt, Customer)
            .join(Customer, Customer.id == Prompt.author_customer_id)
            .where(Prompt.active)
            .order_by(Prompt.created_at.desc())
        )
    ).all()
    my_prompts = (
        await session.execute(
            select(Prompt).where(Prompt.author_customer_id == customer.id).order_by(Prompt.created_at.desc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request, "prompts.html", {"customer": customer, "marketplace": marketplace, "my_prompts": my_prompts}
    )


@app.post("/prompts")
async def create_prompt(
    title: str = Form(...),
    description: str = Form(""),
    system_prompt: str = Form(...),
    price_rub: Decimal = Form(...),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    if price_rub <= 0:
        # Без этой проверки цена промпта уходит прямо в billing.charge_prompt_fee
        # как есть: отрицательная цена = payer.balance_rub -= price_rub увеличивает
        # баланс покупателя при каждом вызове — способ бесконечно печатать себе деньги.
        raise HTTPException(status_code=400, detail="price_rub must be positive")
    session.add(
        Prompt(
            author_customer_id=customer.id,
            title=title.strip(),
            description=description.strip() or None,
            system_prompt=system_prompt.strip(),
            price_rub=price_rub,
        )
    )
    await session.commit()
    return RedirectResponse("/prompts", status_code=303)


# ---------- веб: архиватор диалогов ----------


@app.get("/archive")
async def archive_list(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    archives = (
        await session.execute(
            select(DialogueArchive)
            .where(DialogueArchive.customer_id == customer.id)
            .order_by(DialogueArchive.created_at.desc())
        )
    ).scalars().all()
    return templates.TemplateResponse(request, "archive.html", {"customer": customer, "archives": archives, "error": None})


@app.post("/archive")
async def create_archive(
    request: Request,
    content: str = Form(...),
    label: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing = (
        await session.execute(select(DialogueArchive).where(DialogueArchive.content_hash == content_hash))
    ).scalar_one_or_none()
    if existing is None:
        session.add(DialogueArchive(customer_id=customer.id, content_hash=content_hash, label=label.strip() or None))
        await session.commit()
    archives = (
        await session.execute(
            select(DialogueArchive)
            .where(DialogueArchive.customer_id == customer.id)
            .order_by(DialogueArchive.created_at.desc())
        )
    ).scalars().all()
    note = None if existing is None else "Этот текст уже был сохранён ранее — хэш совпал, новая запись не создана."
    return templates.TemplateResponse(request, "archive.html", {"customer": customer, "archives": archives, "error": note})


@app.get("/verify")
async def verify_form(request: Request, customer: Customer | None = Depends(get_current_customer)):
    return templates.TemplateResponse(request, "verify.html", {"customer": customer, "result": None, "checked": False})


@app.post("/verify")
async def verify_submit(
    request: Request,
    content: str = Form(...),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    row = (
        await session.execute(
            select(DialogueArchive, Customer)
            .join(Customer, Customer.id == DialogueArchive.customer_id)
            .where(DialogueArchive.content_hash == content_hash)
        )
    ).first()
    return templates.TemplateResponse(
        request, "verify.html", {"customer": customer, "result": row, "checked": True}
    )


# ---------- веб: семейный тариф «Репетитор» ----------


@app.get("/children/new")
async def new_child_form(request: Request, customer: Customer | None = Depends(get_current_customer)):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    if customer.is_child:
        raise HTTPException(status_code=403, detail="child accounts cannot create children")
    return templates.TemplateResponse(request, "child_new.html", {"customer": customer, "error": None})


@app.post("/children/new")
async def create_child(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    if customer.is_child:
        raise HTTPException(status_code=403, detail="child accounts cannot create children")
    email = email.strip().lower()
    exists = (await session.execute(select(Customer).where(Customer.email == email))).scalar_one_or_none()
    if exists is not None:
        return templates.TemplateResponse(
            request, "child_new.html", {"customer": customer, "error": "Этот email уже зарегистрирован"}, status_code=409
        )
    session.add(
        Customer(
            email=email,
            name=name.strip(),
            password_hash=hash_password(password),
            is_child=True,
            parent_customer_id=customer.id,
        )
    )
    await session.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/children/{child_id}")
async def view_child(
    child_id: int,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    child = await session.get(Customer, child_id)
    if child is None or child.parent_customer_id != customer.id:
        raise HTTPException(status_code=404)
    events = (
        await session.execute(
            select(UsageEvent)
            .where(UsageEvent.customer_id == child.id)
            .order_by(UsageEvent.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    return templates.TemplateResponse(request, "child_history.html", {"customer": customer, "child": child, "events": events})


# ---------- веб: AI-секретарь (Telegram) ----------


@app.post("/telegram/link")
async def telegram_link_request(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=404, detail="telegram secretary is not configured")
    code = secrets.token_hex(4)
    session.add(TelegramLinkCode(customer_id=customer.id, code=code))
    await session.commit()
    return RedirectResponse(f"/?telegram_code={code}", status_code=303)


# ---------- веб: админ ----------


@app.get("/admin/overview")
async def admin_overview(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect

    # Касса — сколько реально пришло денег (подтверждённые пополнения).
    total_topped_up = (
        await session.execute(
            select(func.coalesce(func.sum(WalletLedger.delta_rub), 0)).where(
                WalletLedger.entry_type == "topup"
            )
        )
    ).scalar_one()
    # Обязательство перед клиентами — сколько ещё могут потратить.
    outstanding_balance = (
        await session.execute(
            select(func.coalesce(func.sum(Customer.balance_rub), 0)).where(Customer.role == "customer")
        )
    ).scalar_one()
    # Признанная выручка — списано за реальное использование токенов.
    recognized_revenue = (
        await session.execute(
            select(func.coalesce(func.sum(-WalletLedger.delta_rub), 0)).where(
                WalletLedger.entry_type == "usage"
            )
        )
    ).scalar_one()
    # Себестоимость у провайдеров, переведённая в рубли по курсу на момент вызова.
    # По charged_rub IS NOT NULL, не по status=='success' — событие с честным
    # списанием по оценке при обрыве стрима (1.3 доработок, billing_estimated)
    # имеет status='failed', но реальная себестоимость и списание у него есть.
    cost_rub = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0)).where(
                UsageEvent.charged_rub.is_not(None)
            )
        )
    ).scalar_one()
    customer_count = (
        await session.execute(select(func.count(Customer.id)).where(Customer.role == "customer"))
    ).scalar_one()
    failed_events = (
        await session.execute(select(func.count(UsageEvent.id)).where(UsageEvent.status == "failed"))
    ).scalar_one()
    uncosted_events = (
        await session.execute(
            select(func.count(UsageEvent.id)).where(
                UsageEvent.status == "success", UsageEvent.cost_usd.is_(None)
            )
        )
    ).scalar_one()

    # 1.6 доработок: маржа по каждой модели, не только общая — изменение
    # цены у поставщика (или ошибка в model_prices) должно быть видно
    # по конкретной модели, а не тонуть в среднем по больнице.
    by_model_rows = (
        await session.execute(
            select(
                UsageEvent.model,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.charged_rub), 0),
                func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0),
            )
            .where(UsageEvent.charged_rub.is_not(None))
            .group_by(UsageEvent.model)
            .order_by(func.count(UsageEvent.id).desc())
        )
    ).all()
    by_model = []
    for model, count, revenue, cost in by_model_rows:
        margin = revenue - cost
        margin_pct = (margin / revenue * 100) if revenue else None
        by_model.append(
            {
                "model": model,
                "count": count,
                "revenue": revenue,
                "cost": cost,
                "margin": margin,
                "margin_pct": margin_pct,
                "low_margin": margin_pct is not None and margin_pct < settings.margin_alert_threshold_pct,
            }
        )

    recent_customers = (
        await session.execute(
            select(Customer)
            .where(Customer.role == "customer")
            .order_by(Customer.created_at.desc())
            .limit(10)
        )
    ).scalars().all()

    return templates.TemplateResponse(
        request,
        "admin_overview.html",
        {
            "customer": customer,
            "settings": settings,
            "total_topped_up": total_topped_up,
            "outstanding_balance": outstanding_balance,
            "recognized_revenue": recognized_revenue,
            "cost_rub": cost_rub,
            "margin_rub": recognized_revenue - cost_rub,
            "customer_count": customer_count,
            "failed_events": failed_events,
            "uncosted_events": uncosted_events,
            "by_model": by_model,
            "recent_customers": recent_customers,
        },
    )


@app.get("/admin/reconciliation")
async def admin_reconciliation(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    d: str | None = None,
):
    """1.5 доработок: ежедневная сверка. У нас нет реальной интеграции с
    биллингом провайдера (депозит и т.п. — не подключены), поэтому сверяем
    себестоимость ДВУМЯ независимыми способами, которые уже считаются на
    каждый вызов: наша cost_usd (по своей таблице model_prices) против
    litellm_cost (встроенная оценка LiteLLM, контрольное значение) — большое
    расхождение сигналит об устаревшем прайсе, а не о реальном перерасходе
    у поставщика. litellm_cost не пишется для стримингового успеха (main.py,
    _stream_chat_completion) — только для нестримингового пути и Telegram,
    поэтому покрытие частичное, это явно показано в отчёте."""
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect

    if d:
        try:
            day = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    else:
        day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    charged_today = UsageEvent.charged_rub.is_not(None) & (UsageEvent.created_at >= day_start) & (
        UsageEvent.created_at < day_end
    )

    revenue = (
        await session.execute(select(func.coalesce(func.sum(UsageEvent.charged_rub), 0)).where(charged_today))
    ).scalar_one()
    cost = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0)).where(charged_today)
        )
    ).scalar_one()
    total_count = (
        await session.execute(select(func.count(UsageEvent.id)).where(charged_today))
    ).scalar_one()

    litellm_covered = charged_today & UsageEvent.litellm_cost.is_not(None)
    litellm_cost_rub = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.litellm_cost * UsageEvent.usd_rub_rate), 0)).where(
                litellm_covered
            )
        )
    ).scalar_one()
    cost_for_covered = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0)).where(litellm_covered)
        )
    ).scalar_one()
    litellm_covered_count = (
        await session.execute(select(func.count(UsageEvent.id)).where(litellm_covered))
    ).scalar_one()

    discrepancy_rub = cost_for_covered - litellm_cost_rub
    discrepancy_pct = (discrepancy_rub / litellm_cost_rub * 100) if litellm_cost_rub else None
    high_discrepancy = (
        discrepancy_pct is not None and abs(discrepancy_pct) > settings.cost_discrepancy_alert_threshold_pct
    )

    margin = revenue - cost
    margin_pct = (margin / revenue * 100) if revenue else None
    low_margin = margin_pct is not None and margin_pct < settings.margin_alert_threshold_pct

    by_model_rows = (
        await session.execute(
            select(
                UsageEvent.model,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.charged_rub), 0),
                func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0),
            )
            .where(charged_today)
            .group_by(UsageEvent.model)
            .order_by(func.count(UsageEvent.id).desc())
        )
    ).all()
    by_model = []
    for model, count, model_revenue, model_cost in by_model_rows:
        model_margin = model_revenue - model_cost
        model_margin_pct = (model_margin / model_revenue * 100) if model_revenue else None
        by_model.append(
            {
                "model": model,
                "count": count,
                "revenue": model_revenue,
                "cost": model_cost,
                "margin": model_margin,
                "margin_pct": model_margin_pct,
                "low_margin": model_margin_pct is not None
                and model_margin_pct < settings.margin_alert_threshold_pct,
            }
        )

    return templates.TemplateResponse(
        request,
        "admin_reconciliation.html",
        {
            "customer": customer,
            "settings": settings,
            "day": day,
            "prev_day": day - timedelta(days=1),
            "next_day": day + timedelta(days=1),
            "is_today": day >= datetime.now(timezone.utc).date(),
            "revenue": revenue,
            "cost": cost,
            "margin": margin,
            "margin_pct": margin_pct,
            "low_margin": low_margin,
            "total_count": total_count,
            "litellm_cost_rub": litellm_cost_rub,
            "cost_for_covered": cost_for_covered,
            "discrepancy_rub": discrepancy_rub,
            "discrepancy_pct": discrepancy_pct,
            "high_discrepancy": high_discrepancy,
            "litellm_covered_count": litellm_covered_count,
            "by_model": by_model,
        },
    )


@app.get("/admin/topups")
async def admin_topups(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    rows = (
        await session.execute(
            select(TopupRequest, Customer)
            .join(Customer, Customer.id == TopupRequest.customer_id)
            .order_by(TopupRequest.status != "requested", TopupRequest.created_at.desc())
        )
    ).all()
    return templates.TemplateResponse(request, "admin_topups.html", {"customer": customer, "rows": rows})


@app.post("/admin/topups/{topup_id}/confirm")
async def admin_confirm_topup(
    topup_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    topup = await session.get(TopupRequest, topup_id)
    if topup is None or topup.status != "requested":
        raise HTTPException(status_code=404)
    await billing.confirm_topup(session, topup, admin_id=customer.id)
    return RedirectResponse("/admin/topups", status_code=303)


@app.post("/admin/topups/{topup_id}/reject")
async def admin_reject_topup(
    topup_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    topup = await session.get(TopupRequest, topup_id)
    if topup is None or topup.status != "requested":
        raise HTTPException(status_code=404)
    await billing.reject_topup(session, topup, admin_id=customer.id)
    return RedirectResponse("/admin/topups", status_code=303)


@app.get("/admin/password-resets")
async def admin_password_resets(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    rows = (
        await session.execute(
            select(PasswordResetRequest, Customer)
            .join(Customer, Customer.id == PasswordResetRequest.customer_id)
            .order_by(PasswordResetRequest.status != "requested", PasswordResetRequest.created_at.desc())
        )
    ).all()
    return templates.TemplateResponse(request, "admin_password_resets.html", {"customer": customer, "rows": rows})


@app.post("/admin/password-resets/{reset_id}/reset")
async def admin_reset_password(
    request: Request,
    reset_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    reset_req = await session.get(PasswordResetRequest, reset_id)
    if reset_req is None or reset_req.status != "requested":
        raise HTTPException(status_code=404)

    target = await session.get(Customer, reset_req.customer_id)
    new_password = generate_temp_password()
    target.password_hash = hash_password(new_password)
    reset_req.status = "completed"
    reset_req.completed_at = utcnow()
    reset_req.completed_by_admin_id = customer.id
    await session.commit()
    return templates.TemplateResponse(
        request,
        "password_reset_shown.html",
        {"customer": customer, "target_email": target.email, "new_password": new_password},
    )


@app.get("/admin/orders")
async def admin_orders(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    rows = (
        await session.execute(
            select(SubscriptionOrder, Product, Customer)
            .join(Product, Product.id == SubscriptionOrder.product_id)
            .join(Customer, Customer.id == SubscriptionOrder.customer_id)
            .order_by(SubscriptionOrder.status != "paid", SubscriptionOrder.created_at.desc())
        )
    ).all()
    return templates.TemplateResponse(request, "admin_orders.html", {"customer": customer, "rows": rows})


@app.post("/admin/orders/{order_id}/fulfill")
async def admin_fulfill_order(
    order_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    order = await session.get(SubscriptionOrder, order_id)
    if order is None or order.status != "paid":
        raise HTTPException(status_code=404)
    await billing.fulfill_order(session, order, admin_id=customer.id)
    return RedirectResponse("/admin/orders", status_code=303)


@app.post("/admin/orders/{order_id}/refund")
async def admin_refund_order(
    order_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    order = await session.get(SubscriptionOrder, order_id)
    if order is None or order.status != "paid":
        raise HTTPException(status_code=404)
    await billing.refund_order(session, order, admin_id=customer.id)
    return RedirectResponse("/admin/orders", status_code=303)


# ---------- веб: админ — люди, потребление, приглашения ----------


def _parse_month(value: str | None) -> tuple[datetime, datetime, str]:
    """'YYYY-MM' -> границы месяца в UTC. По умолчанию — текущий месяц.
    Границы считаем явными сравнениями created_at, а не приведением к дате
    в SQL: приведение timestamptz к date в Postgres зависит от таймзоны
    сессии, и отчёт молча съезжал бы на границах суток."""
    now = datetime.now(timezone.utc)
    if value:
        try:
            parsed = datetime.strptime(value, "%Y-%m")
        except ValueError:
            raise HTTPException(status_code=400, detail="month must be YYYY-MM")
        year, month = parsed.year, parsed.month
    else:
        year, month = now.year, now.month
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)
    return start, end, f"{year:04d}-{month:02d}"


def _shift_month(label: str, delta: int) -> str:
    year, month = (int(part) for part in label.split("-"))
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


async def _spend_by_customer(session: AsyncSession, start: datetime, end: datetime) -> dict:
    """Расход за период по КОШЕЛЬКАМ (billing_customer_id): для детского
    аккаунта платит родитель, и бюджет расходуется у него."""
    rows = (
        await session.execute(
            select(
                UsageEvent.billing_customer_id,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.charged_rub), 0),
            )
            .where(
                UsageEvent.charged_rub.is_not(None),
                UsageEvent.created_at >= start,
                UsageEvent.created_at < end,
            )
            .group_by(UsageEvent.billing_customer_id)
        )
    ).all()
    return {payer_id: {"calls": calls, "spent": spent} for payer_id, calls, spent in rows}


@app.get("/admin/customers")
async def admin_customers(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    month: str | None = None,
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    start, end, label = _parse_month(month)
    spend = await _spend_by_customer(session, start, end)
    customers = (
        await session.execute(
            select(Customer).order_by(Customer.active.desc(), Customer.created_at.desc())
        )
    ).scalars().all()
    rows = [
        {
            "c": c,
            "calls": spend.get(c.id, {}).get("calls", 0),
            "spent": spend.get(c.id, {}).get("spent", Decimal(0)),
        }
        for c in customers
    ]
    rows.sort(key=lambda r: (r["spent"], r["calls"]), reverse=True)
    return templates.TemplateResponse(
        request,
        "admin_customers.html",
        {
            "customer": customer,
            "rows": rows,
            "month": label,
            "prev_month": _shift_month(label, -1),
            "next_month": _shift_month(label, 1),
            "is_current_month": label == _parse_month(None)[2],
            "total_spent": sum((r["spent"] for r in rows), Decimal(0)),
            "total_calls": sum(r["calls"] for r in rows),
        },
    )


@app.get("/admin/customers.csv")
async def admin_customers_csv(
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    month: str | None = None,
):
    """Выгрузка для бухгалтерии/распределения бюджета на следующий месяц —
    иначе цифры пришлось бы переписывать из таблицы руками."""
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    start, end, label = _parse_month(month)
    spend = await _spend_by_customer(session, start, end)
    customers = (
        await session.execute(select(Customer).order_by(Customer.name))
    ).scalars().all()

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Период", "Имя", "Email", "Роль", "Вызовов", "Потрачено, ₽", "Баланс, ₽"])
    for c in customers:
        stats = spend.get(c.id, {})
        writer.writerow(
            [
                label,
                c.name,
                c.email,
                c.role,
                stats.get("calls", 0),
                f"{stats.get('spent', Decimal(0)):.4f}".replace(".", ","),
                f"{c.balance_rub:.4f}".replace(".", ","),
            ]
        )
    # BOM и ; как разделитель — иначе русский Excel открывает файл одной
    # колонкой и портит кириллицу.
    body = "﻿" + buffer.getvalue()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="flawless-{label}.csv"'},
    )


@app.post("/admin/customers/{customer_id}/toggle-active")
async def admin_toggle_customer_active(
    customer_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    """Отключение аккаунта. Действует немедленно и на сессию, и на API-ключи:
    обе зависимости аутентификации перечитывают Customer.active на каждом
    запросе (см. security.py). Раньше поле читалось, но не выставлялось нигде —
    отключить человека можно было только SQL-запросом руками."""
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    target = await session.get(Customer, customer_id)
    if target is None:
        raise HTTPException(status_code=404)
    if target.id == customer.id:
        # Иначе админ отключает сам себя и теряет доступ в админку — вернуть
        # его сможет только SQL, то есть ровно та проблема, что мы чиним.
        raise HTTPException(status_code=400, detail="cannot disable your own account")
    target.active = not target.active
    await session.commit()
    return RedirectResponse("/admin/customers", status_code=303)


@app.get("/admin/customers/{customer_id}")
async def admin_customer_detail(
    customer_id: int,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    month: str | None = None,
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    target = await session.get(Customer, customer_id)
    if target is None:
        raise HTTPException(status_code=404)
    start, end, label = _parse_month(month)

    in_period = (
        UsageEvent.billing_customer_id == target.id,
        UsageEvent.charged_rub.is_not(None),
        UsageEvent.created_at >= start,
        UsageEvent.created_at < end,
    )

    by_model = [
        {"model": model, "calls": calls, "spent": spent, "cost": cost}
        for model, calls, spent, cost in (
            await session.execute(
                select(
                    UsageEvent.model,
                    func.count(UsageEvent.id),
                    func.coalesce(func.sum(UsageEvent.charged_rub), 0),
                    func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0),
                )
                .where(*in_period)
                .group_by(UsageEvent.model)
                .order_by(func.coalesce(func.sum(UsageEvent.charged_rub), 0).desc())
            )
        ).all()
    ]

    # По дням группируем в Python: приведение timestamptz к дате в SQL зависит
    # от таймзоны сессии Postgres, и сутки могли бы съезжать. Данных здесь —
    # события одного человека за один месяц, это дёшево.
    daily: dict[str, dict] = {}
    for created_at, charged in (
        await session.execute(
            select(UsageEvent.created_at, UsageEvent.charged_rub).where(*in_period)
        )
    ).all():
        day = created_at.astimezone(timezone.utc).strftime("%d.%m")
        bucket = daily.setdefault(day, {"calls": 0, "spent": Decimal(0)})
        bucket["calls"] += 1
        bucket["spent"] += charged
    by_day = [{"day": day, **stats} for day, stats in sorted(daily.items(), reverse=True)]

    period_spent = sum((row["spent"] for row in by_model), Decimal(0))
    period_calls = sum(row["calls"] for row in by_model)
    lifetime_spent = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.charged_rub), 0)).where(
                UsageEvent.billing_customer_id == target.id, UsageEvent.charged_rub.is_not(None)
            )
        )
    ).scalar_one()

    ledger = (
        await session.execute(
            select(WalletLedger)
            .where(WalletLedger.customer_id == target.id)
            .order_by(WalletLedger.created_at.desc())
            .limit(50)
        )
    ).scalars().all()

    return templates.TemplateResponse(
        request,
        "admin_customer_detail.html",
        {
            "customer": customer,
            "target": target,
            "ledger": ledger,
            "month": label,
            "prev_month": _shift_month(label, -1),
            "next_month": _shift_month(label, 1),
            "is_current_month": label == _parse_month(None)[2],
            "by_model": by_model,
            "by_day": by_day,
            "period_spent": period_spent,
            "period_calls": period_calls,
            "lifetime_spent": lifetime_spent,
        },
    )


@app.post("/admin/customers/{customer_id}/balance")
async def admin_change_balance(
    customer_id: int,
    amount_rub: Decimal = Form(...),
    entry_type: str = Form("adjustment"),
    note: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    if entry_type not in ("topup", "adjustment"):
        raise HTTPException(status_code=400, detail="entry_type must be topup or adjustment")
    if amount_rub == 0:
        raise HTTPException(status_code=400, detail="amount must not be zero")
    if not note.strip():
        # Ручное движение денег без объяснения через полгода не расшифровать —
        # для того и заводили created_by_admin_id рядом.
        raise HTTPException(status_code=400, detail="note is required for manual balance changes")
    target = await session.get(Customer, customer_id)
    if target is None:
        raise HTTPException(status_code=404)
    await billing.admin_adjust_balance(
        session,
        customer_id=target.id,
        delta_rub=amount_rub,
        entry_type=entry_type,
        note=note.strip(),
        admin_id=customer.id,
    )
    return RedirectResponse(f"/admin/customers/{target.id}", status_code=303)


_PRICING_EXAMPLE_COST_USD = Decimal("0.01")


async def _pricing_page_context(session: AsyncSession, customer: Customer, error: str | None):
    """Пример «во сколько обойдётся вызов» считаем здесь, а не в шаблоне:
    в Jinja литерал 0.01 — float, а markup/rate — Decimal, и их произведение
    роняет рендер TypeError'ом (поймано тестом до боя)."""
    cfg = await billing.get_pricing_config(session)
    updated_by = (
        await session.get(Customer, cfg.updated_by_admin_id) if cfg.updated_by_admin_id else None
    )
    return {
        "customer": customer,
        "cfg": cfg,
        "updated_by": updated_by,
        "error": error,
        "example_rub": billing.price_in_rub(_PRICING_EXAMPLE_COST_USD, cfg),
    }


@app.post("/admin/customers/{customer_id}/limits")
async def admin_set_customer_limits(
    customer_id: int,
    daily_limit_rub: str = Form(""),
    monthly_limit_rub: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    """Потолок расхода на человека. В отличие от лимитов на ключе (их ставит
    сам клиент), этот — бюджетный контроль компании, поэтому только админ.
    Пусто = без потолка."""
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    target = await session.get(Customer, customer_id)
    if target is None:
        raise HTTPException(status_code=404)

    def _parse(raw: str) -> Decimal | None:
        raw = raw.strip()
        if not raw:
            return None
        value = Decimal(raw)
        if value < 0:
            raise HTTPException(status_code=400, detail="limit must not be negative")
        return value

    target.daily_limit_rub = _parse(daily_limit_rub)
    target.monthly_limit_rub = _parse(monthly_limit_rub)
    await session.commit()
    return RedirectResponse(f"/admin/customers/{target.id}", status_code=303)


@app.get("/admin/pricing")
async def admin_pricing(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(
        request, "admin_pricing.html", await _pricing_page_context(session, customer, None)
    )


@app.post("/admin/pricing")
async def admin_update_pricing(
    request: Request,
    markup_percent: Decimal = Form(...),
    usd_rub_rate: Decimal = Form(...),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    if markup_percent < 0 or usd_rub_rate <= 0:
        return templates.TemplateResponse(
            request,
            "admin_pricing.html",
            await _pricing_page_context(
                session,
                customer,
                "Наценка не может быть отрицательной, курс — нулевым или отрицательным",
            ),
            status_code=400,
        )
    await billing.update_pricing_config(session, markup_percent, usd_rub_rate, admin_id=customer.id)
    return RedirectResponse("/admin/pricing", status_code=303)


@app.get("/admin/invites")
async def admin_invites(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    rows = (
        await session.execute(
            select(InviteCode, Customer)
            .outerjoin(Customer, Customer.id == InviteCode.used_by_customer_id)
            .order_by(InviteCode.used_at.is_not(None), InviteCode.created_at.desc())
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "admin_invites.html",
        {"customer": customer, "rows": rows, "signup_mode": settings.signup_mode},
    )


@app.post("/admin/invites/new")
async def admin_create_invite(
    note: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    session.add(
        InviteCode(
            code=secrets.token_urlsafe(8),
            note=note.strip() or None,
            created_by_admin_id=customer.id,
        )
    )
    await session.commit()
    return RedirectResponse("/admin/invites", status_code=303)


# ---------- веб: админ — API-ключи (2.2 доработок) ----------


@app.get("/admin/api-keys")
async def admin_api_keys(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    rows = (
        await session.execute(
            select(ApiKey, Customer)
            .join(Customer, Customer.id == ApiKey.customer_id)
            .where(ApiKey.active)
            .order_by(ApiKey.created_at.desc())
        )
    ).all()
    return templates.TemplateResponse(request, "admin_api_keys.html", {"customer": customer, "rows": rows})


@app.post("/admin/api-keys/{key_id}/limits")
async def admin_set_api_key_limits(
    key_id: int,
    daily_limit_rub: str = Form(""),
    monthly_limit_rub: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    """Потолок админа поверх клиентского (не замена — см.
    billing._effective_limit) — для реакции на подозрительный ключ, не
    дожидаясь, пока клиент сам себя ограничит."""
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404)
    api_key.admin_daily_limit_rub = Decimal(daily_limit_rub) if daily_limit_rub.strip() else None
    api_key.admin_monthly_limit_rub = Decimal(monthly_limit_rub) if monthly_limit_rub.strip() else None
    await session.commit()
    return RedirectResponse("/admin/api-keys", status_code=303)


# ---------- детский тариф «Репетитор» ----------

_CHILD_SYSTEM_PROMPT = (
    "Ты — репетитор. Не давай готовый ответ на задание (сочинение, реферат, доклад, готовое решение "
    "целиком) — вместо этого объясняй тему и задавай наводящие вопросы, чтобы ученик пришёл к ответу сам."
)
_CHILD_BLOCKED_PATTERN = re.compile(
    r"(?i)\b(напиши|сделай|составь|сгенерируй)\b[^.]{0,40}\b(сочинение|реферат|эссе|доклад)\b"
)


class ChildRequestBlocked(Exception):
    pass


def _prepare_messages(customer: Customer, messages: list[dict], prompt: Prompt | None) -> tuple[list[dict], list[str]]:
    """Готовит messages к отправке провайдеру: детский системный промпт и
    блок-лист (если детский аккаунт), системный промпт купленной «роли»,
    DLP-редактирование секретов. Порядок: сначала детский промпт (внешний
    контроль), затем промпт из библиотеки (пользовательский выбор)."""
    prepared = list(messages)

    if customer.is_child:
        last_user_text = next(
            (m.get("content") for m in reversed(prepared) if m.get("role") == "user" and isinstance(m.get("content"), str)),
            "",
        )
        if _CHILD_BLOCKED_PATTERN.search(last_user_text or ""):
            raise ChildRequestBlocked()
        prepared = [{"role": "system", "content": _CHILD_SYSTEM_PROMPT}] + prepared

    if prompt is not None:
        prepared = [{"role": "system", "content": prompt.system_prompt}] + prepared

    prepared, dlp_found = dlp.redact_messages(prepared)
    return prepared, dlp_found


# ---------- API: OpenAI-совместимый чат ----------

# Схема запроса — extra="allow" (см. app/schemas.py), потому что параметры
# вроде temperature/max_tokens прозрачно летят в LiteLLM. Но LiteLLM также
# принимает api_base/api_key/base_url и т.п. как per-call override —
# пропусти их клиенту, и он подменит эндпоинт вызова своим доменом: сервер
# отправит туда наш настоящий ключ провайдера (SSRF + утечка ключа). Поэтому
# белый список, а не чёрный — новый опасный параметр в LiteLLM не появится
# здесь сам по себе. mock_response — официальный тестовый параметр LiteLLM
# (используется в tests/), безопасен: не делает исходящих вызовов.
_ALLOWED_EXTRA_PARAMS = {
    "temperature", "top_p", "max_tokens", "max_completion_tokens",
    "presence_penalty", "frequency_penalty", "stop", "stream",
    "stream_options", "n", "seed", "response_format", "tools", "tool_choice",
    "user", "logprobs", "top_logprobs", "mock_response",
}


def _idempotency_request_hash(model: str, messages: list, prompt_id: int | None) -> str:
    """Повтор с тем же Idempotency-Key, но ДРУГИМ телом запроса — не должен
    молча вернуть чужой кэшированный ответ (находка состязательного ревью
    2026-09-04)."""
    payload = json.dumps({"model": model, "messages": messages, "prompt_id": prompt_id}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _replay_idempotent_response(existing: UsageEvent):
    if existing.status == "pending":
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "message": "request with this Idempotency-Key is still processing",
                    "type": "idempotency_conflict",
                }
            },
        )
    if existing.response_snapshot is not None:
        return JSONResponse(
            status_code=existing.response_status_code or 200,
            content=json.loads(existing.response_snapshot),
        )
    # Стриминговые ответы снапшот не сохраняют (см. _stream_chat_completion) —
    # честно сообщаем, что повтор для них не воспроизводится байт-в-байт,
    # а не тихо отдаём пустой/неверный ответ.
    raise HTTPException(
        status_code=409,
        detail={
            "error": {
                "message": "request with this Idempotency-Key was already processed "
                "(streamed responses cannot be replayed)",
                "type": "idempotency_replay_unavailable",
            }
        },
    )


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    auth: tuple[Customer, ApiKey] = Depends(get_customer_by_api_key),
    session: AsyncSession = Depends(get_session),
):
    customer, api_key = auth

    if not ratelimit.check(ratelimit.api_key_bucket(api_key.id)):
        raise HTTPException(
            status_code=429,
            detail={"error": {"message": "rate limit exceeded, slow down", "type": "rate_limit_error"}},
        )

    # Потолки расхода — до резерва и вызова провайдера, чтобы не тратить
    # деньги на заведомо заблокированный запрос. Проверяются оба уровня:
    # на кошельке (не обходится вторым ключом) и на самом ключе.
    try:
        await billing.check_spend_limits(
            session, billing.resolve_billing_customer_id(customer), api_key
        )
    except billing.SpendLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "message": f"{e.period} spend limit exceeded for this {e.scope}",
                    "type": "spend_limit_exceeded",
                    "scope": e.scope,
                    "limit_rub": str(e.limit),
                    "spent_rub": str(e.spent),
                }
            },
        )

    extra = {
        k: v
        for k, v in body.model_dump(exclude={"model", "messages", "prompt_id"}).items()
        if k in _ALLOWED_EXTRA_PARAMS
    }

    try:
        provider, model = llm.resolve_alias(body.model)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={"error": {"message": f"unknown model '{body.model}'", "type": "invalid_request_error"}},
        )

    prompt = None
    if body.prompt_id is not None:
        prompt = await session.get(Prompt, body.prompt_id)
        if prompt is None or not prompt.active:
            raise HTTPException(
                status_code=404,
                detail={"error": {"message": f"unknown prompt_id {body.prompt_id}", "type": "invalid_request_error"}},
            )

    try:
        messages, dlp_found = _prepare_messages(customer, body.messages, prompt)
    except ChildRequestBlocked:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "Недоступно в детском режиме — попроси объяснить тему, а не готовое сочинение/реферат.",
                    "type": "child_mode_blocked",
                }
            },
        )

    billing_customer_id = billing.resolve_billing_customer_id(customer)

    # Идемпотентность (1.4 доработок): Idempotency-Key — обычный HTTP-заголовок
    # (как у Stripe), не поле тела. По actor'у (customer.id), НЕ по
    # billing_customer_id — иначе два ребёнка одного родителя делили бы одно
    # пространство ключей (находка состязательного ревью 2026-09-04).
    # Проверяем ДО start_call — повтор не должен ни списывать деньги повторно,
    # ни дублировать вызов провайдера. Хэш тела запроса — чтобы повтор с тем
    # же ключом, но ДРУГИМ запросом, не вернул молча чужой кэшированный ответ.
    idempotency_key = (request.headers.get("idempotency-key") or "").strip()[:200] or None
    request_hash = None
    if idempotency_key is not None:
        request_hash = _idempotency_request_hash(body.model, body.messages, body.prompt_id)
        existing = await billing.find_event_by_idempotency_key(session, customer.id, idempotency_key)
        if existing is not None:
            if existing.idempotency_request_hash is not None and existing.idempotency_request_hash != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": {
                            "message": "this Idempotency-Key was already used with a different request",
                            "type": "idempotency_key_reused",
                        }
                    },
                )
            return _replay_idempotent_response(existing)

    pricing_cfg = await billing.get_pricing_config(session)
    reserve_price = await pricing.find_price(session, provider, model, None, None, utcnow())
    reserve_rub = billing.estimate_reserve_rub(
        reserve_price,
        messages,
        extra,
        pricing_cfg,
        extra_fixed_rub=prompt.price_rub if prompt is not None else Decimal(0),
    )

    try:
        event = await billing.start_call(
            session,
            customer.id,
            billing_customer_id,
            provider,
            model,
            estimated_reserve_rub=reserve_rub,
            idempotency_key=idempotency_key,
        )
    except billing.InsufficientBalance as e:
        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "message": "insufficient balance, top up at /",
                    "type": "insufficient_quota",
                    "balance_rub": str(e.balance),
                }
            },
        )
    except IntegrityError:
        # Гонка по Idempotency-Key (см. billing.start_call) — ровно один
        # параллельный запрос с тем же ключом выигрывает INSERT, этот проиграл.
        # НЕ делаем rollback() здесь и не читаем чужую строку в том же
        # запросе — тот же урок, что и в start_call/purchase_subscription:
        # rollback() посреди запроса экспайрит все объекты сессии (включая
        # customer из auth-зависимости), следующее обращение к ним роняет
        # MissingGreenlet. Сессия закроется в конце запроса и откатится сама —
        # проще попросить клиента повторить с тем же ключом, тогда уже
        # предварительная проверка find_event_by_idempotency_key его найдёт.
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "message": "request with this Idempotency-Key conflicted, please retry",
                    "type": "idempotency_conflict",
                }
            },
        )

    if idempotency_key is not None:
        event.idempotency_request_hash = request_hash

    event.api_key_id = api_key.id
    if dlp_found:
        event.dlp_redactions = ",".join(sorted(set(dlp_found)))
    if prompt is not None:
        event.prompt_id = prompt.id

    if extra.get("stream"):
        return StreamingResponse(
            _stream_chat_completion(session, event, body.model, messages, extra, prompt, billing_customer_id),
            media_type="text/event-stream",
        )

    started = time.monotonic()
    try:
        used_alias, provider, model, response = await llm.chat_completion_with_fallback(
            body.model, messages, **extra
        )
    except Exception as e:
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.warning("provider call failed for event %s (incl. fallback chain): %r", event.id, e)
        if isinstance(e, litellm.RateLimitError):
            status_code = 503
        elif isinstance(e, litellm.Timeout):
            status_code = 504
        else:
            status_code = 502
        error_detail = {"error": {"message": str(e), "type": "provider_error"}}
        await billing.finalize_failure(
            session,
            event,
            error_code=type(e).__name__,
            latency_ms=latency_ms,
            response_snapshot=json.dumps(error_detail),
            response_status_code=status_code,
        )
        raise HTTPException(status_code=status_code, detail=error_detail)

    if used_alias != body.model:
        logger.info("event %s served by fallback %s instead of %s", event.id, used_alias, body.model)
    event.provider = provider
    event.model = model

    latency_ms = int((time.monotonic() - started) * 1000)
    usage = llm.extract_chat_usage(response)
    price = await pricing.find_price(session, provider, model, None, None, event.created_at)
    cost_usd = pricing.compute_cost(price, usage)
    if cost_usd is None:
        logger.warning(
            "no cost computed for event %s (%s/%s): check model_prices coverage",
            event.id, provider, model,
        )
    response_dict = llm.to_dict(response)
    await billing.finalize_success(
        session,
        event,
        usage=usage,
        cost_usd=cost_usd,
        price_id=price.id if price else None,
        litellm_cost=llm.extract_litellm_cost(response),
        pricing_cfg=pricing_cfg,
        latency_ms=latency_ms,
        provider_request_id=llm.extract_call_id(response),
        response_snapshot=json.dumps(response_dict),
        response_status_code=200,
    )
    if prompt is not None:
        await billing.charge_prompt_fee(session, billing_customer_id, prompt, event.id)

    return response_dict


async def _stream_chat_completion(
    session: AsyncSession,
    event: UsageEvent,
    alias: str,
    messages: list[dict],
    extra: dict,
    prompt: Prompt | None,
    billing_customer_id: int,
):
    """Генератор для StreamingResponse. FastAPI держит Depends(get_session)
    открытым, пока не будет отправлен весь ответ, так что биллинг после
    последнего чанка отрабатывает в той же сессии, что и start_call.
    Fallback работает только ДО первого чанка (см. llm.chat_completion_with_fallback) —
    обрыв соединения посреди уже отправленного клиенту потока не переигрывается.

    Обрыв ПОСЛЕ того, как клиенту уже ушла часть контента (1.3 доработок):
    списываем оценку по факту уже отправленных чанков (usage от провайдера
    обычно приходит только последним чанком — при обрыве раньше его просто
    нет), а не 0₽, — иначе можно получить бесплатный частичный ответ, просто
    оборвав соединение.

    ВАЖНО (найдено состязательным ревью 2026-09-04): реальный разрыв
    соединения клиентом доставляется в генератор как asyncio.CancelledError
    или GeneratorExit — оба наследуются от BaseException, НЕ от Exception, и
    `except Exception` их не ловит. Поэтому биллинг обрыва здесь завязан на
    внешний `finally`, а не только на `except Exception` — finally отработает
    при ЛЮБОМ способе выйти из функции, иначе pending-событие и его резерв
    зависают навсегда (см. billing.start_call — активный резерв не снимается,
    пока статус не изменится)."""
    stream_kwargs = dict(extra)
    stream_kwargs["stream"] = True
    stream_kwargs.setdefault("stream_options", {"include_usage": True})

    pricing_cfg = await billing.get_pricing_config(session)
    started = time.monotonic()
    usage = pricing.UsageAmounts()
    call_id = None
    content_so_far = ""
    provider: str | None = None
    model: str | None = None
    finalized = False

    async def _charge_partial_and_close(error_code: str) -> None:
        nonlocal finalized
        if finalized:
            return
        partial_usage = None
        partial_cost_usd = None
        partial_price_id = None
        if content_so_far and provider is not None and model is not None:
            partial_price = await pricing.find_price(session, provider, model, None, None, event.created_at)
            partial_usage = pricing.UsageAmounts(
                input_text_tokens=pricing.estimate_messages_tokens(messages),
                output_tokens=pricing.estimate_tokens_from_text(content_so_far),
            )
            partial_cost_usd = pricing.compute_cost(partial_price, partial_usage)
            partial_price_id = partial_price.id if partial_price else None
        await billing.finalize_failure(
            session,
            event,
            error_code=error_code,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=partial_usage,
            cost_usd=partial_cost_usd,
            price_id=partial_price_id,
            pricing_cfg=pricing_cfg,
            estimated=bool(partial_usage),
        )
        finalized = True

    try:
        try:
            used_alias, provider, model, stream = await llm.chat_completion_with_fallback(
                alias, messages, **stream_kwargs
            )
        except Exception as e:
            logger.warning("provider stream failed to start for event %s (incl. fallback chain): %r", event.id, e)
            await _charge_partial_and_close(type(e).__name__)
            yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'provider_error'}})}\n\n"
            yield "data: [DONE]\n\n"
            return

        if used_alias != alias:
            logger.info("event %s (stream) served by fallback %s instead of %s", event.id, used_alias, alias)
        event.provider = provider
        event.model = model

        try:
            async for chunk in stream:
                data = llm.to_dict(chunk)
                call_id = call_id or data.get("id")
                for choice in data.get("choices") or []:
                    delta = choice.get("delta") or {}
                    delta_content = delta.get("content")
                    if isinstance(delta_content, str):
                        content_so_far += delta_content
                    # tool-calling стримит аргументы функции отдельными
                    # дельтами, не через delta.content — без этого обрыв
                    # посреди дорогого tool-call вывода списывался бы 0₽.
                    for tool_call in delta.get("tool_calls") or []:
                        args_fragment = ((tool_call or {}).get("function") or {}).get("arguments")
                        if isinstance(args_fragment, str):
                            content_so_far += args_fragment
                chunk_usage = data.get("usage")
                if chunk_usage:
                    # extract_chat_usage, а не ручная сборка из двух полей —
                    # иначе cached_tokens/cache_write_tokens теряются именно
                    # для стриминга (найдено состязательным ревью 2026-09-04).
                    usage = llm.extract_chat_usage(data)
                yield f"data: {json.dumps(data)}\n\n"
        except Exception as e:
            logger.warning("provider stream interrupted for event %s: %r", event.id, e)
            await _charge_partial_and_close(type(e).__name__)
            yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'provider_error'}})}\n\n"
            yield "data: [DONE]\n\n"
            return

        latency_ms = int((time.monotonic() - started) * 1000)
        price = await pricing.find_price(session, provider, model, None, None, event.created_at)
        cost_usd = pricing.compute_cost(price, usage)
        if cost_usd is None:
            logger.warning(
                "no cost computed for streamed event %s (%s/%s): check model_prices coverage or stream_options.include_usage support",
                event.id, provider, model,
            )
        await billing.finalize_success(
            session,
            event,
            usage=usage,
            cost_usd=cost_usd,
            price_id=price.id if price else None,
            litellm_cost=None,
            pricing_cfg=pricing_cfg,
            latency_ms=latency_ms,
            provider_request_id=call_id,
        )
        finalized = True
        if prompt is not None:
            await billing.charge_prompt_fee(session, billing_customer_id, prompt, event.id)
        yield "data: [DONE]\n\n"
    finally:
        if not finalized:
            # Сюда попадаем при реальном обрыве соединения (CancelledError/
            # GeneratorExit) — yield здесь запрещён (генератор закрывается),
            # только фиксируем биллинг, чтобы резерв не завис навсегда.
            await _charge_partial_and_close("ClientDisconnected")


# ---------- веб: чат в кабинете (2.3 доработок) ----------
# Третья дверь входа рядом с API-ключом и Telegram — для клиентов, которые
# никогда не видели API-ключа. Тот же путь биллинга, что и /v1/chat/completions
# (billing.estimate_reserve_rub/start_call/finalize_*) — никакой отдельной
# логики списания, только другая обвязка вокруг тех же функций.

_CHAT_ALLOWED_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log"}
_CHAT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # с запасом под фото; не под видео/архивы

# Парсинг PDF/офисных документов НЕ реализован (нужна доп. библиотека,
# scope не указывал конкретные форматы) — только картинки (vision) и простой
# текст. См. CLAUDE.md.


def _chat_message_display_text(content: str) -> str:
    """content может быть JSON-списком частей (текст+картинка, OpenAI-формат) —
    для истории в интерфейсе достаточно текстовой части."""
    if not content.startswith("["):
        return content
    try:
        parts = json.loads(content)
    except (ValueError, TypeError):
        return content
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
    return "\n".join(t for t in texts if t) or "[вложение]"


def _chat_message_provider_content(content: str):
    if content.startswith("["):
        try:
            return json.loads(content)
        except (ValueError, TypeError):
            return content
    return content


@app.get("/chat")
async def web_chat(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    conversations = (
        await session.execute(
            select(WebConversation)
            .where(WebConversation.customer_id == customer.id)
            .order_by(WebConversation.updated_at.desc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "customer": customer,
            "conversations": conversations,
            "active_conversation": None,
            "chat_messages": [],
            "models": llm.known_models(),
        },
    )


@app.get("/chat/{conversation_id}")
async def web_chat_conversation(
    conversation_id: int,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    conversation = await session.get(WebConversation, conversation_id)
    if conversation is None or conversation.customer_id != customer.id:
        raise HTTPException(status_code=404)
    conversations = (
        await session.execute(
            select(WebConversation)
            .where(WebConversation.customer_id == customer.id)
            .order_by(WebConversation.updated_at.desc())
        )
    ).scalars().all()
    raw_messages = (
        await session.execute(
            select(WebMessage)
            .where(WebMessage.conversation_id == conversation.id)
            .order_by(WebMessage.created_at)
        )
    ).scalars().all()
    chat_messages = [
        {"role": m.role, "text": _chat_message_display_text(m.content), "attachment_name": m.attachment_name}
        for m in raw_messages
    ]
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "customer": customer,
            "conversations": conversations,
            "active_conversation": conversation,
            "chat_messages": chat_messages,
            "models": llm.known_models(),
        },
    )


@app.post("/chat/send")
async def web_chat_send(
    conversation_id: int | None = Form(None),
    model: str = Form(...),
    message: str = Form(""),
    file: UploadFile | None = File(None),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        raise HTTPException(status_code=401)

    # Те же ограничители, что и у API-двери: раньше веб-чат не проверял
    # ни частоту, ни потолок расхода — лимит обходился переходом сюда.
    # Частота считается по человеку (ключа здесь нет), потолок — по кошельку.
    if not ratelimit.check(ratelimit.customer_bucket(customer.id)):
        raise HTTPException(status_code=429, detail="Слишком часто — подождите немного")
    try:
        await billing.check_spend_limits(session, billing.resolve_billing_customer_id(customer))
    except billing.SpendLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=f"Достигнут лимит расхода ({e.period}): потрачено {e.spent} ₽ из {e.limit} ₽",
        )

    try:
        provider, model_name = llm.resolve_alias(model)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown model '{model}'")

    conversation = None
    if conversation_id is not None:
        conversation = await session.get(WebConversation, conversation_id)
        if conversation is None or conversation.customer_id != customer.id:
            raise HTTPException(status_code=404)

    if not message.strip() and file is None:
        raise HTTPException(status_code=400, detail="empty message")

    attachment_name = None
    content_parts = None  # None -> просто текст; иначе список частей (текст+картинка)
    extra_text = ""
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > _CHAT_MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="file too large (5MB max)")
        attachment_name = file.filename
        content_type = file.content_type or ""
        ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if content_type.startswith("image/"):
            b64 = base64.b64encode(data).decode("ascii")
            content_parts = [
                {"type": "text", "text": message.strip() or "Что на этом изображении?"},
                {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64}"}},
            ]
        elif ext in _CHAT_ALLOWED_TEXT_EXTENSIONS:
            try:
                extra_text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(status_code=400, detail="text file must be UTF-8")
        else:
            raise HTTPException(
                status_code=400,
                detail="unsupported file type — only images and text files (.txt/.md/.csv/.json/.log)",
            )

    if content_parts is not None:
        stored_content = json.dumps(content_parts)
    else:
        user_text = message.strip()
        if extra_text:
            user_text = f"{user_text}\n\nПрикреплённый файл {attachment_name}:\n{extra_text}".strip()
        stored_content = user_text

    if conversation is None:
        title = (message.strip() or attachment_name or "Новый диалог")[:60]
        conversation = WebConversation(customer_id=customer.id, model_alias=model, title=title)
        session.add(conversation)
        await session.flush()
    conversation.model_alias = model
    conversation.updated_at = utcnow()

    history = (
        await session.execute(
            select(WebMessage)
            .where(WebMessage.conversation_id == conversation.id)
            .order_by(WebMessage.created_at)
        )
    ).scalars().all()
    provider_messages = [
        {"role": m.role, "content": _chat_message_provider_content(m.content)} for m in history
    ]
    provider_messages.append(
        {"role": "user", "content": content_parts if content_parts is not None else stored_content}
    )

    try:
        prepared_messages, dlp_found = _prepare_messages(customer, provider_messages, None)
    except ChildRequestBlocked:
        raise HTTPException(
            status_code=400,
            detail="Недоступно в детском режиме — попроси объяснить тему, а не готовое сочинение/реферат.",
        )

    session.add(
        WebMessage(
            conversation_id=conversation.id, role="user", content=stored_content, attachment_name=attachment_name
        )
    )
    await session.flush()

    billing_customer_id = billing.resolve_billing_customer_id(customer)
    pricing_cfg = await billing.get_pricing_config(session)
    reserve_price = await pricing.find_price(session, provider, model_name, None, None, utcnow())
    reserve_rub = billing.estimate_reserve_rub(reserve_price, prepared_messages, {}, pricing_cfg)

    try:
        event = await billing.start_call(
            session, customer.id, billing_customer_id, provider, model_name, estimated_reserve_rub=reserve_rub
        )
    except billing.InsufficientBalance as e:
        raise HTTPException(
            status_code=402,
            detail=f"insufficient balance — нужно ~{e.required} ₽, доступно {e.balance} ₽",
        )
    if dlp_found:
        event.dlp_redactions = ",".join(sorted(set(dlp_found)))

    started = time.monotonic()
    try:
        _, resp_provider, resp_model, response = await llm.chat_completion_with_fallback(model, prepared_messages)
    except Exception as e:
        latency_ms = int((time.monotonic() - started) * 1000)
        await billing.finalize_failure(session, event, error_code=type(e).__name__, latency_ms=latency_ms)
        logger.warning("web chat call failed for event %s (incl. fallback chain): %r", event.id, e)
        raise HTTPException(status_code=502, detail="провайдер сейчас недоступен, попробуйте ещё раз")

    event.provider = resp_provider
    event.model = resp_model
    latency_ms = int((time.monotonic() - started) * 1000)
    usage = llm.extract_chat_usage(response)
    price = await pricing.find_price(session, resp_provider, resp_model, None, None, event.created_at)
    cost_usd = pricing.compute_cost(price, usage)
    reply_text = llm.to_dict(response)["choices"][0]["message"]["content"]
    await billing.finalize_success(
        session,
        event,
        usage=usage,
        cost_usd=cost_usd,
        price_id=price.id if price else None,
        litellm_cost=llm.extract_litellm_cost(response),
        pricing_cfg=pricing_cfg,
        latency_ms=latency_ms,
        provider_request_id=llm.extract_call_id(response),
    )

    session.add(WebMessage(conversation_id=conversation.id, role="assistant", content=reply_text, usage_event_id=event.id))
    await session.commit()

    return JSONResponse(
        {"conversation_id": conversation.id, "conversation_title": conversation.title, "reply": reply_text}
    )
