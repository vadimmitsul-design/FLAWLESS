import hashlib
import secrets

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import ApiKey, Customer


MIN_PASSWORD_LENGTH = 8


def password_problem(password: str) -> str | None:
    """Почему такой пароль брать нельзя — или None, если можно.

    Длина не проверялась нигде: ни при регистрации, ни в scripts/manage_admin.py,
    где `--password` объявлен обязательным, но пустая строка проходит как
    значение. Один раз так уже завели администратора с ПУСТЫМ паролем — путь
    к временному файлу не существовал, переменная оболочки оказалась пустой,
    и скрипт молча это принял.
    """
    if not password or not password.strip():
        return "пароль не может быть пустым"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"пароль короче {MIN_PASSWORD_LENGTH} символов"
    return None


def hash_password(password: str) -> str:
    problem = password_problem(password)
    if problem is not None:
        # Последний рубеж: хешировать заведомо негодный пароль нельзя даже
        # если вызывающий забыл проверить. Дешевле упасть здесь, чем завести
        # учётку, в которую войдёт кто угодно.
        raise ValueError(problem)
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    return "nh_" + secrets.token_urlsafe(32)


def generate_temp_password() -> str:
    return secrets.token_urlsafe(9)


async def get_current_customer(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Customer | None:
    customer_id = request.session.get("customer_id")
    if customer_id is None:
        return None
    customer = await session.get(Customer, customer_id)
    if customer is None or not customer.active:
        return None
    return customer


async def get_customer_by_api_key(
    request: Request, session: AsyncSession = Depends(get_session)
) -> tuple[Customer, ApiKey]:
    """Возвращает (Customer, ApiKey) — конкретный ключ нужен вызывающему коду
    для лимитов расхода НА КЛЮЧ (2.2 доработок), не только для опознания
    клиента."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"error": {"message": "missing bearer token", "type": "invalid_request_error"}})
    token = auth.removeprefix("Bearer ").strip()
    stmt = (
        select(Customer, ApiKey)
        .join(ApiKey, ApiKey.customer_id == Customer.id)
        .where(ApiKey.key_hash == hash_api_key(token), ApiKey.active, Customer.active)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid API key", "type": "invalid_request_error"}})
    return row[0], row[1]
