"""Employee resource pages and request input adapters."""

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _feature_resources, get_current_customer
from app.core.config import settings
from app.db import get_session
from app.db.models import Customer, Resource
from app.services import resources

router = APIRouter()


@router.get("/resources", dependencies=[Depends(_feature_resources)])
async def my_resources(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request,
        "resources.html",
        {
            "customer": customer,
            "items": await resources.resources_with_state(session, owner_id=customer.id),
            "requests": await resources.customer_requests(session, customer.id),
            "kinds": Resource.KINDS,
            "kind_titles": resources.RESOURCE_KIND_TITLES,
            "warn_days": settings.resource_expiry_warn_days,
        },
    )


@router.post("/resources/request", dependencies=[Depends(_feature_resources)])
async def request_resource(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    kind: str = Form("subscription"),
    name: str = Form(...),
    provider: str = Form(""),
    account: str = Form(""),
    period_months: str = Form(""),
    estimated_amount: str = Form(""),
    currency: str = Form("RUB"),
    reason: str = Form(""),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    months = None
    if period_months.strip():
        try:
            months = int(period_months)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="срок: нужно число") from exc
    amount = None
    if estimated_amount.strip():
        try:
            amount = Decimal(estimated_amount.replace(",", "."))
        except (InvalidOperation, ValueError) as exc:
            raise HTTPException(status_code=400, detail="сумма: нужно число") from exc
    await resources.request_resource(
        session,
        customer.id,
        resources.RequestDetails(
            kind=kind,
            name=name,
            provider=provider,
            account=account,
            period_months=months,
            estimated_amount=amount,
            currency=currency,
            reason=reason,
        ),
    )
    await session.commit()
    return RedirectResponse("/resources", status_code=303)
