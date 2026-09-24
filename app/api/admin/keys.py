"""api / admin / keys for the Flawless application."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _require_admin, get_current_customer
from app.api.validation import money_field
from app.db import get_session
from app.db.models import (
    Customer,
)
from app.services import api_keys

router = APIRouter()


# ---------- веб: админ — API-ключи (2.2 доработок) ----------


@router.get("/admin/api-keys")
async def admin_api_keys(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    rows = await api_keys.active_keys_with_owners(session)
    return request.app.state.templates.TemplateResponse(
        request, "admin_api_keys.html", {"customer": customer, "rows": rows}
    )


@router.post("/admin/api-keys/{key_id}/limits")
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
    assert customer is not None
    await api_keys.set_admin_limits(
        session,
        key_id,
        daily=money_field(daily_limit_rub, "потолок в день"),
        monthly=money_field(monthly_limit_rub, "потолок в месяц"),
    )
    await session.commit()
    return RedirectResponse("/admin/api-keys", status_code=303)
