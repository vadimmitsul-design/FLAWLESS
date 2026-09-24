"""api / admin / customers for the Flawless application."""

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

from app.api.dependencies import _require_admin, get_current_customer
from app.api.validation import money_field
from app.core.formatting import csv_cell
from app.db import get_session
from app.db.models import (
    Customer,
)
from app.services import customers as customers_service
from app.services import wallet
from app.services.customers import customer_detail, customer_report

router = APIRouter()


@router.get("/admin/customers")
async def admin_customers(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    month: str | None = None,
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    context = await customer_report(session, month)
    context["customer"] = customer
    return request.app.state.templates.TemplateResponse(request, "admin_customers.html", context)


@router.get("/admin/customers.csv")
async def admin_customers_csv(
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    month: str | None = None,
):
    """Выгрузка для бухгалтерии/распределения бюджета на следующий месяц —
    иначе цифры пришлось бы переписывать из таблицы руками."""
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    report = await customers_service.customer_export(session, month)
    label, spend, customers = report["month"], report["spend"], report["customers"]

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Период", "Имя", "Email", "Роль", "Вызовов", "Потрачено, ₽", "Баланс, ₽"])
    for c in customers:
        stats = spend.get(c.id, {})
        writer.writerow(
            [
                label,
                csv_cell(c.name),
                csv_cell(c.email),
                c.role,
                stats.get("calls", 0),
                f"{stats.get('spent', Decimal(0)):.4f}".replace(".", ","),
                f"{c.balance_rub:.4f}".replace(".", ","),
            ]
        )
    # BOM и ; как разделитель — иначе русский Excel открывает файл одной
    # колонкой и портит кириллицу.
    body = "﻿" + buffer.getvalue()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="flawless-{label}.csv"'},
    )


@router.post("/admin/customers/{customer_id}/toggle-active")
async def admin_toggle_customer_active(
    customer_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    """Отключение аккаунта. Действует немедленно и на сессию, и на API-ключи:
    обе зависимости аутентификации перечитывают Customer.active на каждом
    запросе (см. security.py). Раньше поле читалось, но не выставлялось нигде —
    отключить человека можно было только SQL-запросом руками."""
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    await customers_service.toggle_active(session, customer_id, customer.id)
    await session.commit()
    return RedirectResponse("/admin/customers", status_code=303)


@router.get("/admin/customers/{customer_id}")
async def admin_customer_detail(
    customer_id: int,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    month: str | None = None,
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    context = await customer_detail(session, customer_id, month)
    context["customer"] = customer
    return request.app.state.templates.TemplateResponse(
        request, "admin_customer_detail.html", context
    )


@router.post("/admin/customers/{customer_id}/balance")
async def admin_change_balance(
    customer_id: int,
    amount_rub: Decimal = Form(...),
    entry_type: str = Form("adjustment"),
    note: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    await wallet.adjust_balance(
        session,
        customer_id,
        amount=amount_rub,
        entry_type=entry_type,
        note=note,
        admin_id=customer.id,
    )
    return RedirectResponse(f"/admin/customers/{customer_id}", status_code=303)


@router.post("/admin/customers/{customer_id}/card")
async def admin_customer_card(
    customer_id: int,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
    name: str = Form(...),
    job_title: str = Form(""),
    department: str = Form(""),
):
    """Кто этот человек в компании.

    ФИО правит администратор, потому что сам человек вписывает себе `name`
    при регистрации в поле с подписью «Имя / компания» — во внутреннем
    контуре там оказывается «Вадим» или «я», а на дашборде сроков нужно
    понимать, кому продлевать подписку.
    """
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    await customers_service.update_card(
        session, customer_id, name=name, job_title=job_title, department=department
    )
    await session.commit()
    return RedirectResponse(f"/admin/customers/{customer_id}", status_code=303)


@router.post("/admin/customers/{customer_id}/limits")
async def admin_set_customer_limits(
    customer_id: int,
    daily_limit_rub: str = Form(""),
    monthly_limit_rub: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    """Потолок расхода на человека. В отличие от лимитов на ключе (их ставит
    сам клиент), этот — бюджетный контроль компании, поэтому только админ.
    Пусто = без потолка."""
    redirect = _require_admin(customer)
    if redirect is not None:
        return redirect
    assert customer is not None
    await customers_service.set_limits(
        session,
        customer_id,
        daily=money_field(daily_limit_rub, "daily limit"),
        monthly=money_field(monthly_limit_rub, "monthly limit"),
    )
    await session.commit()
    return RedirectResponse(f"/admin/customers/{customer_id}", status_code=303)
