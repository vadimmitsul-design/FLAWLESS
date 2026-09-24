"""Session-authenticated JSON adapter for the Next.js customer cabinet."""

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_customer
from app.core import ratelimit
from app.db import get_session
from app.db.models import Customer
from app.services import api_keys, auth, cabinet_json, wallet

router = APIRouter(prefix="/cabinet-api", tags=["cabinet"])


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class KeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=120)


class TopupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_rub: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
    note: str = Field(default="", max_length=1000)


async def _authenticated(
    customer: Annotated[Customer | None, Depends(get_current_customer)],
) -> Customer:
    if customer is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return customer


CurrentCustomer = Annotated[Customer, Depends(_authenticated)]
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


@router.get("/session")
async def session_info(customer: CurrentCustomer) -> dict[str, Any]:
    return cabinet_json.customer_summary(customer)


@router.post("/login")
async def login(data: LoginInput, request: Request, session: DatabaseSession) -> dict[str, Any]:
    client_ip = request.client.host if request.client else "unknown"
    if not ratelimit.check_login(client_ip):
        raise HTTPException(status_code=429, detail="Слишком много попыток входа, попробуйте позже")
    customer = await auth.authenticate(session, data.email, data.password)
    if customer is None:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    request.session.clear()
    request.session["customer_id"] = customer.id
    return cabinet_json.customer_summary(customer)


@router.post("/logout")
async def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"ok": True}


@router.get("/dashboard")
async def dashboard(
    request: Request, customer: CurrentCustomer, session: DatabaseSession
) -> dict[str, Any]:
    return await cabinet_json.dashboard(session, customer, request.app.state.settings)


@router.get("/conversations/{conversation_id}")
async def conversation(
    conversation_id: int, customer: CurrentCustomer, session: DatabaseSession
) -> dict[str, Any]:
    return await cabinet_json.conversation(session, customer.id, conversation_id)


@router.post("/keys", status_code=201)
async def create_key(
    data: KeyInput, customer: CurrentCustomer, session: DatabaseSession
) -> dict[str, str]:
    raw_key = await api_keys.issue_key(session, customer.id, data.name)
    await session.commit()
    return {"raw_key": raw_key}


@router.delete("/keys/{key_id}")
async def revoke_key(
    key_id: int, customer: CurrentCustomer, session: DatabaseSession
) -> dict[str, bool]:
    await api_keys.revoke_key(session, key_id, customer.id)
    await session.commit()
    return {"ok": True}


@router.post("/topups", status_code=201)
async def request_topup(
    data: TopupInput, customer: CurrentCustomer, session: DatabaseSession
) -> dict[str, bool]:
    if customer.is_child:
        raise HTTPException(
            status_code=403, detail="Пополнить общий баланс может владелец аккаунта"
        )
    await wallet.request_topup(session, customer.id, data.amount_rub, data.note)
    await session.commit()
    return {"ok": True}
