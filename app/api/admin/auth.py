"""api / admin / auth for the Flawless application."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _require_admin, get_current_customer
from app.db import get_session
from app.db.models import (
    Customer,
)
from app.services import auth as auth_service

router = APIRouter()


@router.get("/admin/password-resets")
async def admin_password_resets(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    rows = await auth_service.password_reset_requests(session)
    return request.app.state.templates.TemplateResponse(
        request, "admin_password_resets.html", {"customer": customer, "rows": rows}
    )


@router.post("/admin/password-resets/{reset_id}/reset")
async def admin_reset_password(
    request: Request,
    reset_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    result = await auth_service.complete_password_reset(session, reset_id, customer.id)
    await session.commit()
    return request.app.state.templates.TemplateResponse(
        request,
        "password_reset_shown.html",
        {"customer": customer, "target_email": result.email, "new_password": result.password},
    )
