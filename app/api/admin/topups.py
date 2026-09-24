"""api / admin / topups for the Flawless application."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _require_admin, get_current_customer
from app.db import get_session
from app.db.models import (
    Customer,
)
from app.services import wallet

router = APIRouter()


@router.get("/admin/topups")
async def admin_topups(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    rows = await wallet.topup_requests(session)
    return request.app.state.templates.TemplateResponse(
        request, "admin_topups.html", {"customer": customer, "rows": rows}
    )


@router.post("/admin/topups/{topup_id}/confirm")
async def admin_confirm_topup(
    topup_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    await wallet.confirm_topup(session, topup_id, customer.id)
    return RedirectResponse("/admin/topups", status_code=303)


@router.post("/admin/topups/{topup_id}/reject")
async def admin_reject_topup(
    topup_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    await wallet.reject_topup(session, topup_id, customer.id)
    return RedirectResponse("/admin/topups", status_code=303)
