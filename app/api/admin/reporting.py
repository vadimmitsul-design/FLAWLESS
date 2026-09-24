"""api / admin / reporting for the Flawless application."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _require_admin, get_current_customer
from app.db import get_session
from app.db.models import (
    Customer,
)
from app.services.admin_reporting import overview_context, reconciliation_context

router = APIRouter()


# ---------- веб: админ ----------


@router.get("/admin/overview")
async def admin_overview(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None

    # Касса — сколько реально пришло денег (подтверждённые пополнения).
    context = await overview_context(session)
    context["customer"] = customer
    return request.app.state.templates.TemplateResponse(request, "admin_overview.html", context)


@router.get("/admin/reconciliation")
async def admin_reconciliation(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    d: str | None = None,
):
    """1.5 доработок: ежедневная сверка. У нас нет реальной интеграции с
    биллингом провайдера (депозит и т.п. — не подключены), поэтому сверяем
    себестоимость ДВУМЯ независимыми способами, которые уже считаются на
    каждый вызов: наша cost_usd (по своей таблице model_prices) против
    litellm_cost (встроенная оценка LiteLLM, контрольное значение) — большое
    расхождение сигналит об устаревшем прайсе, а не о реальном перерасходе
    у поставщика. litellm_cost не пишется для стримингового успеха (main.py,
    _stream_chat_completion) — только для нестримингового пути и Telegram,
    поэтому покрытие частичное, это явно показано в отчёте."""
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None

    context = await reconciliation_context(session, d)
    context["customer"] = customer
    return request.app.state.templates.TemplateResponse(
        request, "admin_reconciliation.html", context
    )
