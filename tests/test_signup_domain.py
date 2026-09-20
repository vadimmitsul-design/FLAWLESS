# -*- coding: utf-8 -*-
"""Самостоятельная регистрация сотрудников только с корпоративной почты.

Внутренний контур переведён с приглашений на открытую регистрацию, но
ограниченную доменом: завести аккаунт может любой сотрудник компании, и
никто снаружи.

Чем это НЕ является — почта не подтверждается, писем сервис не шлёт вовсе.
Проверка отсекает посторонних и опечатки, но не доказывает, что человек
владеет ящиком. Ущерб ограничен тем, что новый аккаунт заводится с нулевым
балансом; отдельный тест это стережёт.

Префикс адресов — `dom_`: тестовая база одна на весь прогон.
"""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import allowed_signup_domains, email_domain_allowed, settings
from app.db import SessionLocal
from app.models import Customer

CORP = "rossi.ru"


def _exists(email):
    async def _get():
        async with SessionLocal() as session:
            return (
                await session.execute(select(Customer).where(Customer.email == email))
            ).scalar_one_or_none()

    return asyncio.run(_get())


# ---------- разбор настройки ----------


def test_setting_is_parsed_forgivingly():
    """«@» можно не писать, регистр и пробелы не важны, лишние запятые тоже."""
    assert allowed_signup_domains("@rossi.ru, Rossi.COM , ") == ["rossi.ru", "rossi.com"]
    assert allowed_signup_domains("") == []
    assert allowed_signup_domains(None) == []


@pytest.mark.parametrize(
    "email,allowed",
    [
        ("vm@rossi.ru", True),
        ("VM@Rossi.RU", True),
        ("vm@gmail.com", False),
        ("vm@sub.rossi.ru", False),
        ("vm@rossi.ru.evil.com", False),
        ("", False),
        ("без-собаки", False),
    ],
)
def test_domain_match_is_exact(email, allowed):
    """Главное здесь — «rossi.ru.evil.com» и «sub.rossi.ru» НЕ проходят:
    сравнивается домен целиком, а не вхождение подстроки."""
    assert email_domain_allowed(email, CORP) is allowed


def test_empty_setting_means_any_mail_not_none():
    """Клиентский контур работает без ограничения, и пустая строка не должна
    означать «никому нельзя»."""
    assert email_domain_allowed("кто-угодно@gmail.com", "") is True


# ---------- сама регистрация ----------


@pytest.fixture
def corporate(monkeypatch):
    monkeypatch.setattr(settings, "signup_mode", "open")
    monkeypatch.setattr(settings, "signup_allowed_email_domains", CORP)


def test_employee_with_corporate_mail_registers_himself(client, corporate):
    r = client.post(
        "/signup",
        data={"email": "dom_ok@rossi.ru", "name": "Сотрудник", "password": "RabochiyPass123"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert _exists("dom_ok@rossi.ru") is not None


def test_outside_mail_is_refused(client, corporate):
    r = client.post(
        "/signup",
        data={"email": "dom_no@gmail.com", "name": "Чужой", "password": "RabochiyPass123"},
    )
    assert r.status_code == 400
    assert "rossi.ru" in r.text, "человеку не сказали, какая почта нужна"
    assert _exists("dom_no@gmail.com") is None, "аккаунт всё-таки создан"


def test_lookalike_domains_do_not_pass(client, corporate):
    """Ровно тот случай, ради которого сравнение идёт по последнему домену,
    а не поиском подстроки."""
    for email in ("dom_a@rossi.ru.evil.com", "dom_b@notrossi.ru", "dom_c@sub.rossi.ru"):
        assert client.post(
            "/signup", data={"email": email, "name": "x", "password": "RabochiyPass123"}
        ).status_code == 400
        assert _exists(email) is None


def test_case_and_spaces_do_not_let_anyone_in_or_keep_anyone_out(client, corporate):
    r = client.post(
        "/signup",
        data={"email": "  DOM_Case@Rossi.RU  ", "name": "Регистр", "password": "RabochiyPass123"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert _exists("dom_case@rossi.ru") is not None, "почта не приведена к нижнему регистру"


def test_the_form_says_which_mail_is_needed_before_you_type(client, corporate):
    """Узнавать об ограничении после отправки формы — значит терять человека
    на первом же шаге."""
    html = client.get("/signup").text
    assert "@rossi.ru" in html
    assert "рабочей почтой" in html


def test_a_fresh_account_cannot_spend_anything(client, corporate):
    """Опора всей конструкции: домен не подтверждает личность, поэтому важно,
    что самозаписавшийся аккаунт бесполезен без решения администратора."""
    client.post(
        "/signup",
        data={"email": "dom_fresh@rossi.ru", "name": "Новичок", "password": "RabochiyPass123"},
        follow_redirects=True,
    )
    person = _exists("dom_fresh@rossi.ru")
    assert person is not None
    assert person.balance_rub == Decimal("0.0000")

    r = client.post("/chat/send", data={"model": "gpt-5-mini", "message": "привет"})
    assert r.status_code == 402, f"вызов без баланса прошёл: {r.status_code}"


def test_restriction_does_not_leak_into_the_client_contour(client, monkeypatch):
    """Код общий на два контура: клиентский регистрирует кого угодно."""
    monkeypatch.setattr(settings, "signup_mode", "open")
    monkeypatch.setattr(settings, "signup_allowed_email_domains", "")
    r = client.post(
        "/signup",
        data={"email": "dom_client@example.com", "name": "Клиент", "password": "RabochiyPass123"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert _exists("dom_client@example.com") is not None


def test_invite_mode_still_wins_when_it_is_on(client, monkeypatch):
    """Ограничение по домену — не замена приглашениям: если контур закрыт
    кодами, корпоративная почта сама по себе не пускает."""
    monkeypatch.setattr(settings, "signup_mode", "invite")
    monkeypatch.setattr(settings, "signup_allowed_email_domains", CORP)
    r = client.post(
        "/signup",
        data={"email": "dom_invite@rossi.ru", "name": "Без кода", "password": "RabochiyPass123"},
    )
    assert r.status_code == 400
    assert _exists("dom_invite@rossi.ru") is None
