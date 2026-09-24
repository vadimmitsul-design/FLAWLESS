"""api / cabinet for the Flawless application."""

import csv
import io
from decimal import Decimal

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Request,
    Response,
)
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_customer
from app.api.validation import money_field
from app.core.config import (
    settings,
)
from app.core.formatting import csv_cell
from app.db import get_session
from app.db.models import (
    Customer,
)
from app.integrations import llm
from app.services import api_keys, wallet
from app.services.cabinet import dashboard_context
from app.services.catalog import public_page_context
from app.services.reporting import USAGE_PAGE_LIMIT, usage_history

router = APIRouter()


@router.get("/")
async def dashboard(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        # Внутренний контур витрины не имеет: сотруднику нечего продавать,
        # ему нужен вход. Лендинг там был бы просто мусором на главной.
        if not settings.enable_public_site:
            return RedirectResponse("/login", status_code=303)
        return request.app.state.templates.TemplateResponse(
            request, "landing.html", await public_page_context(session)
        )

    context = await dashboard_context(session, customer)
    context["customer"] = customer
    context["telegram_code"] = request.query_params.get("telegram_code")
    return request.app.state.templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/usage")
async def usage_page(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    key: str = "",
    month: str | None = None,
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    ctx = await usage_history(session, customer, key, month, limit=USAGE_PAGE_LIMIT)
    ctx["customer"] = customer
    ctx["limit"] = USAGE_PAGE_LIMIT
    return request.app.state.templates.TemplateResponse(request, "usage.html", ctx)


@router.get("/usage.csv")
async def usage_csv(
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    key: str = "",
    month: str | None = None,
):
    """Выгрузка истории вызовов — то, что витрина обещает приложить к акту.

    Без limit: выгрузка на то и выгрузка, чтобы отдать всё за период.
    """
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    data = await usage_history(session, customer, key, month)
    by_id = {k.id: k for k in data["keys"]}

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        ["Когда", "Модель", "Ключ", "Входящих токенов", "Исходящих", "Списано, ₽", "Статус"]
    )
    for e in data["events"]:
        key_name = "—"
        if e.api_key_id is not None:
            k = by_id.get(e.api_key_id)
            key_name = (k.name or f"ключ {k.id}") if k else f"ключ {e.api_key_id}"
        writer.writerow(
            [
                e.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                csv_cell(llm.alias_for(e.provider, e.model) or e.model),
                csv_cell(key_name),
                e.input_tokens or 0,
                e.output_tokens or 0,
                f"{(e.charged_rub or Decimal(0)):.4f}".replace(".", ","),
                e.status,
            ]
        )
    # BOM и ; как разделитель — иначе русский Excel открывает файл одной
    # колонкой и портит кириллицу (то же, что в админской выгрузке).
    body = "\ufeff" + buffer.getvalue()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="flawless-usage-{data["month"]}.csv"'
        },
    )


@router.post("/api-key/regenerate")
async def create_api_key(
    request: Request,
    name: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    """Название маршрута сохранено для обратной совместимости (документация/
    закладки), поведение — уже не "перевыпуск", а "ещё один именованный
    ключ" (2.1 доработок): старые ключи больше не деактивируются."""
    if customer is None:
        return RedirectResponse("/login", status_code=303)

    raw_key = await api_keys.issue_key(session, customer.id, name)
    await session.commit()
    return request.app.state.templates.TemplateResponse(
        request, "api_key_shown.html", {"customer": customer, "raw_key": raw_key}
    )


@router.post("/api-keys/{key_id}/revoke")
async def revoke_api_key(
    key_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    await api_keys.revoke_key(session, key_id, customer.id)
    await session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/api-keys/{key_id}/limits")
async def set_api_key_limits(
    key_id: int,
    daily_limit_rub: str = Form(""),
    monthly_limit_rub: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    """Клиент настраивает СВОИ лимиты (2.2 доработок) — пусто = лимита нет.
    Не может снять/обойти admin_*_limit_rub — тот проверяется отдельно как
    потолок поверх (см. billing.check_api_key_spend_limits)."""
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    await api_keys.set_customer_limits(
        session,
        key_id,
        customer.id,
        daily=money_field(daily_limit_rub, "лимит в день"),
        monthly=money_field(monthly_limit_rub, "лимит в месяц"),
    )
    await session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/topups/new")
async def new_topup(
    amount_rub: Decimal = Form(...),
    note: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    await wallet.request_topup(session, customer.id, amount_rub, note)
    await session.commit()
    return RedirectResponse("/", status_code=303)
