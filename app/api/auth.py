"""api / auth for the Flawless application."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ratelimit
from app.core.config import (
    allowed_signup_domains,
    settings,
)
from app.core.errors import InvalidInput, StateConflict
from app.db import get_session
from app.services import auth as auth_service

router = APIRouter()


# ---------- веб: регистрация / логин ----------


@router.get("/signup")
async def signup_form(request: Request):
    if settings.signup_mode == "closed":
        return request.app.state.templates.TemplateResponse(
            request, "signup_closed.html", {}, status_code=403
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "signup.html",
        {
            "error": None,
            "invite_required": settings.signup_mode == "invite",
            "allowed_domains": allowed_signup_domains(settings.signup_allowed_email_domains),
        },
    )


@router.post("/signup")
async def signup_submit(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    invite: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    if settings.signup_mode == "closed":
        return request.app.state.templates.TemplateResponse(
            request, "signup_closed.html", {}, status_code=403
        )
    invite_required = settings.signup_mode == "invite"

    allowed_domains = allowed_signup_domains(settings.signup_allowed_email_domains)

    def _fail(message: str, status_code: int):
        return request.app.state.templates.TemplateResponse(
            request,
            "signup.html",
            {
                "error": message,
                "invite_required": invite_required,
                "allowed_domains": allowed_domains,
            },
            status_code=status_code,
        )

    try:
        customer = await auth_service.register_customer(
            session,
            email=email,
            name=name,
            password=password,
            invite=invite,
            config=settings,
        )
    except (InvalidInput, StateConflict) as exc:
        return _fail(exc.message, 409 if isinstance(exc, StateConflict) else 400)

    await session.commit()
    request.session["customer_id"] = customer.id
    return RedirectResponse("/", status_code=303)


@router.get("/login")
async def login_form(request: Request):
    return request.app.state.templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    client_ip = request.client.host if request.client else "unknown"
    if not ratelimit.check_login(client_ip):
        return request.app.state.templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Слишком много попыток входа, попробуйте позже"},
            status_code=429,
        )
    customer = await auth_service.authenticate(session, email, password)
    if customer is None:
        return request.app.state.templates.TemplateResponse(
            request, "login.html", {"error": "Неверный email или пароль"}, status_code=401
        )
    request.session["customer_id"] = customer.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/forgot-password")
async def forgot_password_form(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "forgot_password.html", {"sent": False}
    )


@router.post("/forgot-password")
async def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    # Единственный инструмент восстановления пароля — очередь у администратора,
    # и наливать в неё мог кто угодно без ограничений: страница заявок тонет,
    # настоящая заявка теряется среди мусора. Ключ по IP — тот же, что у входа:
    # кто именно шлёт, здесь неизвестно, это и подбирают.
    if not ratelimit.check_login(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="слишком много попыток, подождите")
    await auth_service.request_password_reset(session, email)
    await session.commit()
    # Один и тот же ответ независимо от того, найден email или нет —
    # иначе форма превращается в способ проверить, кто зарегистрирован.
    return request.app.state.templates.TemplateResponse(
        request, "forgot_password.html", {"sent": True}
    )
