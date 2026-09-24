"""api / archive for the Flawless application."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _feature_archive, get_current_customer
from app.db import get_session
from app.db.models import Customer
from app.services import archive as archive_service

router = APIRouter()


# ---------- веб: архиватор диалогов ----------


@router.get("/archive", dependencies=[Depends(_feature_archive)])
async def archive_list(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    archives = await archive_service.customer_archives(session, customer.id)
    return request.app.state.templates.TemplateResponse(
        request, "archive.html", {"customer": customer, "archives": archives, "error": None}
    )


@router.post("/archive", dependencies=[Depends(_feature_archive)])
async def create_archive(
    request: Request,
    content: str = Form(...),
    label: str = Form(""),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    created = await archive_service.create_archive(session, customer.id, content, label)
    if created:
        await session.commit()
    archives = await archive_service.customer_archives(session, customer.id)
    note = (
        None
        if created
        else "Этот текст уже был сохранён ранее — хэш совпал, новая запись не создана."
    )
    return request.app.state.templates.TemplateResponse(
        request, "archive.html", {"customer": customer, "archives": archives, "error": note}
    )


@router.get("/verify", dependencies=[Depends(_feature_archive)])
async def verify_form(
    request: Request, customer: Customer | None = Depends(get_current_customer)
) -> Response:
    return request.app.state.templates.TemplateResponse(
        request, "verify.html", {"customer": customer, "result": None, "checked": False}
    )


@router.post("/verify", dependencies=[Depends(_feature_archive)])
async def verify_submit(
    request: Request,
    content: str = Form(...),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    row = await archive_service.verify(session, content)
    return request.app.state.templates.TemplateResponse(
        request, "verify.html", {"customer": customer, "result": row, "checked": True}
    )
