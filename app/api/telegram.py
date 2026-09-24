"""api / telegram for the Flawless application."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_customer
from app.core.config import (
    settings,
)
from app.db import get_session
from app.db.models import (
    Customer,
)
from app.services.auth import create_telegram_link_code

router = APIRouter()


# ---------- веб: AI-секретарь (Telegram) ----------


@router.post("/telegram/link")
async def telegram_link_request(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=404, detail="telegram secretary is not configured")
    code = await create_telegram_link_code(session, customer.id)
    await session.commit()
    return RedirectResponse(f"/?telegram_code={code}", status_code=303)
