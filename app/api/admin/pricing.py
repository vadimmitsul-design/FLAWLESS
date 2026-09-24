"""api / admin / pricing for the Flawless application."""

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _require_admin, get_current_customer
from app.db import get_session
from app.db.models import (
    Customer,
)
from app.services import billing
from app.services.pricing_settings import pricing_settings_context

router = APIRouter()


@router.get("/admin/pricing")
async def admin_pricing(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    return request.app.state.templates.TemplateResponse(
        request, "admin_pricing.html", await pricing_settings_context(session, customer, None)
    )


@router.post("/admin/pricing")
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
    assert customer is not None
    if markup_percent < 0 or usd_rub_rate <= 0:
        return request.app.state.templates.TemplateResponse(
            request,
            "admin_pricing.html",
            await pricing_settings_context(
                session,
                customer,
                "Наценка не может быть отрицательной, курс — нулевым или отрицательным",
            ),
            status_code=400,
        )
    await billing.update_pricing_config(session, markup_percent, usd_rub_rate, admin_id=customer.id)
    return RedirectResponse("/admin/pricing", status_code=303)
