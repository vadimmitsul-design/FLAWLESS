"""api / children for the Flawless application."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _feature_children, get_current_customer
from app.core.errors import InvalidInput, StateConflict
from app.db import get_session
from app.db.models import Customer
from app.services import children

router = APIRouter()


# ---------- веб: семейный тариф «Репетитор» ----------


@router.get("/children/new", dependencies=[Depends(_feature_children)])
async def new_child_form(
    request: Request, customer: Customer | None = Depends(get_current_customer)
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    children.require_parent(customer)
    return request.app.state.templates.TemplateResponse(
        request, "child_new.html", {"customer": customer, "error": None}
    )


@router.post("/children/new", dependencies=[Depends(_feature_children)])
async def create_child(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    try:
        await children.create_child(session, customer, email, name, password)
    except (InvalidInput, StateConflict) as exc:
        return request.app.state.templates.TemplateResponse(
            request,
            "child_new.html",
            {"customer": customer, "error": exc.message},
            status_code=409 if isinstance(exc, StateConflict) else 400,
        )
    await session.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/children/{child_id}", dependencies=[Depends(_feature_children)])
async def view_child(
    child_id: int,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    history = await children.child_history(session, customer.id, child_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "child_history.html",
        {"customer": customer, "child": history.child, "events": history.events},
    )
