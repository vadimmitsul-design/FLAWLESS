"""api / shop for the Flawless application."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _feature_shop, get_current_customer
from app.db import get_session
from app.db.models import Customer
from app.services import billing
from app.services import shop as shop_service

router = APIRouter()


# ---------- веб: магазин подписок (платёжный агент) ----------


@router.get("/shop", dependencies=[Depends(_feature_shop)])
async def shop(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    page = await shop_service.shop_page(session, customer.id)
    return request.app.state.templates.TemplateResponse(
        request,
        "shop.html",
        {"customer": customer, "products": page.products, "orders": page.orders, "error": None},
    )


@router.post("/shop/order", dependencies=[Depends(_feature_shop)])
async def shop_order(
    request: Request,
    product_id: int = Form(...),
    account_email: str = Form(...),
    note: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    try:
        await shop_service.purchase(session, customer.id, product_id, account_email, note)
    except billing.InsufficientBalance as e:
        page = await shop_service.shop_page(session, customer.id)
        return request.app.state.templates.TemplateResponse(
            request,
            "shop.html",
            {
                "customer": customer,
                "products": page.products,
                "orders": page.orders,
                "error": f"Недостаточно средств: на балансе {e.balance} ₽, нужно {e.required} ₽. Пополните баланс в кабинете.",
            },
            status_code=402,
        )
    return RedirectResponse("/shop", status_code=303)
