"""Создание/обновление админ-аккаунта (подтверждает пополнения баланса).

Запуск:
  docker compose exec neurohub python scripts/manage_admin.py \
    --email vadim@neurohub.ru --name "Вадим" --password ...

Повторный запуск с тем же email обновляет имя/пароль и роль на admin.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Customer
from app.security import hash_password, password_problem


async def main(email: str, name: str, password: str) -> None:
    email = email.strip().lower()
    async with SessionLocal() as session:
        customer = (
            await session.execute(select(Customer).where(Customer.email == email))
        ).scalar_one_or_none()
        if customer is None:
            session.add(
                Customer(name=name, email=email, password_hash=hash_password(password), role="admin")
            )
            print(f"created admin '{email}'")
        else:
            customer.name = name
            customer.password_hash = hash_password(password)
            customer.role = "admin"
            customer.active = True
            print(f"updated admin '{email}'")
        await session.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()
    problem = password_problem(args.password)
    if problem is not None:
        # argparse с required=True требует, чтобы флаг БЫЛ, но пустое значение
        # пропускает. Так однажды и завели администратора без пароля.
        parser.error(f"--password: {problem}")
    asyncio.run(main(args.email, args.name, args.password))
