"""api / prompts for the Flawless application."""

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _feature_prompts, get_current_customer
from app.db import get_session
from app.db.models import Customer
from app.services import prompts as prompt_service

router = APIRouter()


# ---------- веб: библиотека промптов ----------


@router.get("/prompts", dependencies=[Depends(_feature_prompts)])
async def prompts_list(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    page = await prompt_service.prompt_page(session, customer.id)
    return request.app.state.templates.TemplateResponse(
        request,
        "prompts.html",
        {"customer": customer, "marketplace": page.marketplace, "my_prompts": page.my_prompts},
    )


@router.post("/prompts", dependencies=[Depends(_feature_prompts)])
async def create_prompt(
    title: str = Form(...),
    description: str = Form(""),
    system_prompt: str = Form(...),
    price_rub: Decimal = Form(...),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    prompt_service.create_prompt(session, customer, title, description, system_prompt, price_rub)
    await session.commit()
    return RedirectResponse("/prompts", status_code=303)
