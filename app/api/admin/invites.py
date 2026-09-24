"""api / admin / invites for the Flawless application."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _require_admin, get_current_customer
from app.core.config import (
    settings,
)
from app.db import get_session
from app.db.models import (
    Customer,
)
from app.services import auth as auth_service

router = APIRouter()


@router.get("/admin/invites")
async def admin_invites(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    rows = await auth_service.list_invitations(session)
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_invites.html",
        {"customer": customer, "rows": rows, "signup_mode": settings.signup_mode},
    )


@router.post("/admin/invites/new")
async def admin_create_invite(
    note: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    await auth_service.create_invitation(session, customer.id, note)
    await session.commit()
    return RedirectResponse("/admin/invites", status_code=303)
