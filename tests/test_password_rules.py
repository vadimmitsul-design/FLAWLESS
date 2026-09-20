# -*- coding: utf-8 -*-
"""Длина пароля не проверялась нигде.

Ни при регистрации, ни в scripts/manage_admin.py, где `--password` объявлен
обязательным, но пустая строка проходит как значение — так однажды и завели
администратора с ПУСТЫМ паролем. Пока сервис жил на localhost, это было
терпимо; на сервере, куда ходят сотрудники, — нет.

Префикс адресов — `pwd_`: тестовая база одна на весь прогон.
"""

import asyncio

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Customer
from app.security import MIN_PASSWORD_LENGTH, hash_password, password_problem


def test_empty_and_short_passwords_are_refused():
    assert password_problem("") is not None
    assert password_problem("   ") is not None, "пробелы — это тоже пустой пароль"
    assert password_problem("x" * (MIN_PASSWORD_LENGTH - 1)) is not None
    assert password_problem("x" * MIN_PASSWORD_LENGTH) is None


def test_hashing_refuses_a_password_that_should_never_exist():
    """Последний рубеж: даже если вызывающий забыл проверить, завести учётку,
    в которую войдёт кто угодно, нельзя."""
    with pytest.raises(ValueError):
        hash_password("")
    with pytest.raises(ValueError):
        hash_password("   ")
    with pytest.raises(ValueError):
        hash_password("1234567")  # на один символ короче минимума
    assert hash_password("ДлинныйПароль123")


def test_signup_rejects_a_short_password_without_creating_an_account(client):
    r = client.post(
        "/signup",
        data={"email": "pwd_short@test.local", "name": "Короткий", "password": "123"},
    )
    assert r.status_code == 400, f"короткий пароль принят: {r.status_code}"

    async def _exists():
        async with SessionLocal() as session:
            return (
                await session.execute(
                    select(Customer).where(Customer.email == "pwd_short@test.local")
                )
            ).scalar_one_or_none()

    assert asyncio.run(_exists()) is None, "аккаунт всё-таки создан"


def test_signup_still_works_with_a_normal_password(client):
    r = client.post(
        "/signup",
        data={"email": "pwd_ok@test.local", "name": "Нормальный", "password": "NormalPass123"},
        follow_redirects=True,
    )
    assert r.status_code == 200
