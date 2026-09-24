"""Administrator resource pages and transaction boundaries."""

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _feature_resources, _require_admin, get_current_customer
from app.api.validation import parse_date
from app.core.config import settings
from app.db import get_session
from app.db.models import Customer, Resource
from app.services import resources

router = APIRouter()


@router.get("/admin/resources", dependencies=[Depends(_feature_resources)])
async def admin_resources(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    show: str = "active",
    f: str = "",
    owner: str = "",
) -> Response:
    redirect = _require_admin(customer)
    if redirect:
        return redirect
    owner_id = None
    if owner.strip():
        try:
            owner_id = int(owner)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="владелец указан неверно") from exc
    dashboard = await resources.resource_dashboard(
        session, include_archived=(show == "all"), filter_code=f, owner_id=owner_id
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_resources.html",
        {
            **dashboard,
            "customer": customer,
            "kinds": Resource.KINDS,
            "kind_titles": resources.RESOURCE_KIND_TITLES,
            "show": show,
            "warn_days": settings.resource_expiry_warn_days,
        },
    )


@router.post("/admin/resources/new", dependencies=[Depends(_feature_resources)])
async def admin_resource_create(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    kind: str = Form("proxy"),
    name: str = Form(...),
    provider: str = Form(""),
    owner_customer_id: int = Form(...),
    account: str = Form(""),
    url: str = Form(""),
    expires_at: str = Form(""),
    note: str = Form(""),
) -> Response:
    redirect = _require_admin(customer)
    if redirect:
        return redirect
    assert customer is not None
    await resources.create_resource(
        session,
        resources.ResourceDetails(
            name=name,
            owner_customer_id=owner_customer_id,
            account=account,
            url=url,
            expires_at=parse_date(expires_at),
            note=note,
        ),
        kind=kind,
        provider=provider,
        admin_id=customer.id,
    )
    await session.commit()
    return RedirectResponse("/admin/resources", status_code=303)


@router.post("/admin/resources/{resource_id}/edit", dependencies=[Depends(_feature_resources)])
async def admin_resource_edit(
    resource_id: int,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    name: str = Form(...),
    owner_customer_id: int = Form(...),
    account: str = Form(""),
    url: str = Form(""),
    expires_at: str = Form(""),
    note: str = Form(""),
) -> Response:
    redirect = _require_admin(customer)
    if redirect:
        return redirect
    await resources.edit_resource(
        session,
        resource_id,
        resources.ResourceDetails(
            name=name,
            owner_customer_id=owner_customer_id,
            account=account,
            url=url,
            expires_at=parse_date(expires_at),
            note=note,
        ),
    )
    await session.commit()
    return RedirectResponse("/admin/resources", status_code=303)


@router.post("/admin/resources/{resource_id}/pay", dependencies=[Depends(_feature_resources)])
async def admin_resource_pay(
    resource_id: int,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    amount: Decimal = Form(...),
    currency: str = Form("RUB"),
    paid_at: str = Form(""),
    period_end: str = Form(...),
    note: str = Form(""),
) -> Response:
    redirect = _require_admin(customer)
    if redirect:
        return redirect
    assert customer is not None
    await resources.record_payment(
        session,
        resource_id,
        resources.PaymentDetails(
            amount=amount,
            currency=currency,
            paid_at=parse_date(paid_at),
            period_end=parse_date(period_end),
            note=note,
        ),
        admin_id=customer.id,
    )
    await session.commit()
    return RedirectResponse("/admin/resources", status_code=303)


@router.post("/admin/resources/{resource_id}/archive", dependencies=[Depends(_feature_resources)])
async def admin_resource_archive(
    resource_id: int,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    redirect = _require_admin(customer)
    if redirect:
        return redirect
    await resources.toggle_archive(session, resource_id)
    await session.commit()
    return RedirectResponse("/admin/resources", status_code=303)


@router.post(
    "/admin/resource-requests/{request_id}/reject", dependencies=[Depends(_feature_resources)]
)
async def admin_request_reject(
    request_id: int,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    decision_note: str = Form(""),
) -> Response:
    redirect = _require_admin(customer)
    if redirect:
        return redirect
    assert customer is not None
    await resources.reject_request(
        session, request_id, admin_id=customer.id, decision_note=decision_note
    )
    await session.commit()
    return RedirectResponse("/admin/resources", status_code=303)


@router.post(
    "/admin/resource-requests/{request_id}/fulfil", dependencies=[Depends(_feature_resources)]
)
async def admin_request_fulfil(
    request_id: int,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    amount: Decimal = Form(...),
    currency: str = Form("RUB"),
    period_end: str = Form(...),
    url: str = Form(""),
    decision_note: str = Form(""),
) -> Response:
    redirect = _require_admin(customer)
    if redirect:
        return redirect
    assert customer is not None
    await resources.fulfil_request(
        session,
        request_id,
        resources.PaymentDetails(
            amount=amount,
            currency=currency,
            period_end=parse_date(period_end),
            note=decision_note,
        ),
        admin_id=customer.id,
        url=url,
    )
    await session.commit()
    return RedirectResponse("/admin/resources", status_code=303)
