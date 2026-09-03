import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import litellm
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app import billing, dlp, llm, pricing, ratelimit, telegram_bot
from app.csrf import CSRFOriginMiddleware
from app.config import settings
from app.db import get_session
from app.models import (
    ApiKey,
    Customer,
    DialogueArchive,
    PasswordResetRequest,
    Product,
    Prompt,
    SubscriptionOrder,
    TelegramLink,
    TelegramLinkCode,
    TopupRequest,
    UsageEvent,
    WalletLedger,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.environment == "production" and settings.session_secret == "change-me":
        # Дефолтный секрет в проде = любая сессия подделывается — падаем на
        # старте, а не тихо работаем с дырявыми куками.
        raise RuntimeError("SESSION_SECRET must be set to a real value when ENVIRONMENT=production")
    llm.init_router()
    telegram_task = None
    if settings.telegram_bot_token:
        telegram_task = asyncio.create_task(telegram_bot.poll_loop())
        logger.info("telegram bot polling task started")
    yield
    if telegram_task is not None:
        telegram_task.cancel()


app = FastAPI(title="neurohub", lifespan=lifespan)
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
    return templates.TemplateResponse(request, "signup.html", {"error": None})


@app.post("/signup")
async def signup_submit(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    email = email.strip().lower()
    exists = (
        await session.execute(select(Customer).where(Customer.email == email))
    ).scalar_one_or_none()
    if exists is not None:
        return templates.TemplateResponse(
            request, "signup.html", {"error": "Этот email уже зарегистрирован"}, status_code=409
        )
    customer = Customer(email=email, name=name.strip(), password_hash=hash_password(password))
    session.add(customer)
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

    has_key = (
        await session.execute(
            select(ApiKey).where(ApiKey.customer_id == customer.id, ApiKey.active)
        )
    ).scalar_one_or_none() is not None
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
            "has_key": has_key,
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
async def regenerate_api_key(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)

    await session.execute(
        ApiKey.__table__.update().where(ApiKey.customer_id == customer.id).values(active=False)
    )
    raw_key = generate_api_key()
    session.add(ApiKey(customer_id=customer.id, key_hash=hash_api_key(raw_key)))
    await session.commit()
    return templates.TemplateResponse(
        request, "api_key_shown.html", {"customer": customer, "raw_key": raw_key}
    )


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
    cost_rub = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0)).where(
                UsageEvent.status == "success"
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

    by_model = (
        await session.execute(
            select(
                UsageEvent.model,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.charged_rub), 0),
            )
            .where(UsageEvent.status == "success")
            .group_by(UsageEvent.model)
            .order_by(func.count(UsageEvent.id).desc())
        )
    ).all()

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


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    customer: Customer = Depends(get_customer_by_api_key),
    session: AsyncSession = Depends(get_session),
):
    if not ratelimit.check(customer.id):
        raise HTTPException(
            status_code=429,
            detail={"error": {"message": "rate limit exceeded, slow down", "type": "rate_limit_error"}},
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

    try:
        event = await billing.start_call(session, customer.id, billing_customer_id, provider, model)
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
        await billing.finalize_failure(session, event, error_code=type(e).__name__, latency_ms=latency_ms)
        logger.warning("provider call failed for event %s (incl. fallback chain): %r", event.id, e)
        if isinstance(e, litellm.RateLimitError):
            status_code = 503
        elif isinstance(e, litellm.Timeout):
            status_code = 504
        else:
            status_code = 502
        raise HTTPException(
            status_code=status_code,
            detail={"error": {"message": str(e), "type": "provider_error"}},
        )

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
    pricing_cfg = await billing.get_pricing_config(session)
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
    if prompt is not None:
        await billing.charge_prompt_fee(session, billing_customer_id, prompt, event.id)

    return llm.to_dict(response)


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
    """
    stream_kwargs = dict(extra)
    stream_kwargs["stream"] = True
    stream_kwargs.setdefault("stream_options", {"include_usage": True})

    started = time.monotonic()
    usage = pricing.UsageAmounts()
    call_id = None
    try:
        used_alias, provider, model, stream = await llm.chat_completion_with_fallback(
            alias, messages, **stream_kwargs
        )
    except Exception as e:
        latency_ms = int((time.monotonic() - started) * 1000)
        await billing.finalize_failure(session, event, error_code=type(e).__name__, latency_ms=latency_ms)
        logger.warning("provider stream failed to start for event %s (incl. fallback chain): %r", event.id, e)
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
            chunk_usage = data.get("usage")
            if chunk_usage:
                usage = pricing.UsageAmounts(
                    input_text_tokens=chunk_usage.get("prompt_tokens"),
                    output_tokens=chunk_usage.get("completion_tokens"),
                )
            yield f"data: {json.dumps(data)}\n\n"
    except Exception as e:
        latency_ms = int((time.monotonic() - started) * 1000)
        await billing.finalize_failure(session, event, error_code=type(e).__name__, latency_ms=latency_ms)
        logger.warning("provider stream interrupted for event %s: %r", event.id, e)
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
    pricing_cfg = await billing.get_pricing_config(session)
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
    if prompt is not None:
        await billing.charge_prompt_fee(session, billing_customer_id, prompt, event.id)
    yield "data: [DONE]\n\n"
