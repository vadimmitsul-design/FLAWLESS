"""api / admin / shop for the Flawless application."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _feature_shop, _require_admin, get_current_customer
from app.db import get_session
from app.db.models import Customer
from app.services import billing, shop

router = APIRouter()


@router.get("/admin/orders", dependencies=[Depends(_feature_shop)])
async def admin_orders(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    rows = await shop.all_orders(session)
    return request.app.state.templates.TemplateResponse(
        request, "admin_orders.html", {"customer": customer, "rows": rows}
    )


@router.post("/admin/orders/{order_id}/fulfill", dependencies=[Depends(_feature_shop)])
async def admin_fulfill_order(
    order_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    order = await shop.paid_order(session, order_id)
    await billing.fulfill_order(session, order, admin_id=customer.id)
    return RedirectResponse("/admin/orders", status_code=303)


@router.post("/admin/orders/{order_id}/refund", dependencies=[Depends(_feature_shop)])
async def admin_refund_order(
    order_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    order = await shop.paid_order(session, order_id)
    await billing.refund_order(session, order, admin_id=customer.id)
    return RedirectResponse("/admin/orders", status_code=303)
