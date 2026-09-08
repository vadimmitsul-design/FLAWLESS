"""Автотесты на две денежные дыры из аудита 2026-09-07, которые могли двигать
реальные деньги неправильно даже внутри компании:

  1. детский аккаунт перекачивал кошелёк родителя себе через роялти за
     платный промпт (в аудите воспроизведено на живом приложении);
  2. покупка в магазине не видела активных резервов — одни и те же рубли
     тратились дважды.

Хелперы продублированы намеренно (см. test_features_wave2.py)."""

import asyncio
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import billing
from app.db import SessionLocal
from app.models import Customer, Product, Prompt

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


def _signup(client, email, name="Test User", password="TestPass123"):
    r = client.post(
        "/signup", data={"email": email, "name": name, "password": password}, follow_redirects=True
    )
    assert r.status_code == 200


def _admin_client():
    from fastapi.testclient import TestClient
    from app.main import app

    admin = TestClient(app)
    r = admin.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code in (200, 303)
    return admin


def _new_client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def _customer(email):
    async def _get():
        async with SessionLocal() as session:
            return (
                await session.execute(select(Customer).where(Customer.email == email))
            ).scalar_one()

    return asyncio.run(_get())


def _balance(email):
    return _customer(email).balance_rub


def _fund(admin, email, amount="1000"):
    target = _customer(email)
    admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": amount, "entry_type": "adjustment", "note": "бюджет для теста"},
    )
    return target


def _issue_key(client):
    r = client.post("/api-key/regenerate", data={"name": "k"})
    return re.search(r"nh_[A-Za-z0-9_-]+", r.text).group(0)


def _make_prompt(author_id, price="100.00", title="Промпт"):
    async def _create():
        async with SessionLocal() as session:
            prompt = Prompt(
                author_customer_id=author_id,
                title=title,
                system_prompt="Ты помощник",
                price_rub=Decimal(price),
            )
            session.add(prompt)
            await session.commit()
            return prompt.id

    return asyncio.run(_create())


def _call_with_prompt(client, api_key, prompt_id):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "привет"}],
            "prompt_id": prompt_id,
            "mock_response": "ответ",
        },
    )


# ---------- 1. перекачка кошелька родителя ----------


def test_child_cannot_siphon_the_parent_wallet_via_prompt_royalty(client):
    """Ровно тот сценарий, что воспроизводился в аудите: 1000 ₽ у родителя
    превращались в 899.99 у него и +50 у ребёнка за один вызов."""
    _signup(client, "leak_siphon_parent@test.local")
    admin = _admin_client()
    parent = _fund(admin, "leak_siphon_parent@test.local", "1000")
    client.post(
        "/children/new",
        data={"email": "leak_siphon_kid@test.local", "name": "Ребёнок", "password": "KidPass123"},
        follow_redirects=True,
    )
    kid = _customer("leak_siphon_kid@test.local")

    # Промпт создаём в обход интерфейса: сам маршрут детям теперь запрещён,
    # но проверяем, что даже при наличии такой строки деньги не текут.
    prompt_id = _make_prompt(kid.id, "100.00", "Детский промпт")

    kid_client = _new_client()
    kid_client.post("/login", data={"email": "leak_siphon_kid@test.local", "password": "KidPass123"})
    kid_key = _issue_key(kid_client)

    parent_before = _balance("leak_siphon_parent@test.local")
    r = _call_with_prompt(kid_client, kid_key, prompt_id)
    assert r.status_code == 200

    # Ребёнку не пришло ничего
    assert _balance("leak_siphon_kid@test.local") == Decimal("0.0000")
    # С родителя списана только токенная стоимость, без 100 ₽ за промпт
    spent = parent_before - _balance("leak_siphon_parent@test.local")
    assert spent < Decimal("1.0000"), f"списано {spent} ₽ — похоже на плату за промпт"


def test_repeated_calls_do_not_drain_the_parent(client):
    _signup(client, "leak_siphon2_parent@test.local")
    admin = _admin_client()
    _fund(admin, "leak_siphon2_parent@test.local", "1000")
    client.post(
        "/children/new",
        data={"email": "leak_siphon2_kid@test.local", "name": "Ребёнок", "password": "KidPass123"},
        follow_redirects=True,
    )
    kid = _customer("leak_siphon2_kid@test.local")
    prompt_id = _make_prompt(kid.id, "100.00")

    kid_client = _new_client()
    kid_client.post("/login", data={"email": "leak_siphon2_kid@test.local", "password": "KidPass123"})
    kid_key = _issue_key(kid_client)

    for _ in range(5):
        assert _call_with_prompt(kid_client, kid_key, prompt_id).status_code == 200

    assert _balance("leak_siphon2_kid@test.local") == Decimal("0.0000")
    assert _balance("leak_siphon2_parent@test.local") > Decimal("990.0000")


def test_own_prompt_is_free_for_its_author(client):
    """Платить самому себе за свой промпт незачем — ни платы, ни роялти."""
    _signup(client, "leak_ownprompt@test.local")
    admin = _admin_client()
    target = _fund(admin, "leak_ownprompt@test.local", "1000")
    prompt_id = _make_prompt(target.id, "100.00")
    api_key = _issue_key(client)

    before = _balance("leak_ownprompt@test.local")
    assert _call_with_prompt(client, api_key, prompt_id).status_code == 200
    spent = before - _balance("leak_ownprompt@test.local")
    assert spent < Decimal("1.0000")


def test_royalty_to_an_unrelated_author_still_works(client):
    """Чинили дыру, а не саму механику: чужому автору роялти по-прежнему идёт."""
    author_client = _new_client()
    _signup(author_client, "leak_author@test.local")
    author = _customer("leak_author@test.local")
    prompt_id = _make_prompt(author.id, "100.00", "Чужой промпт")

    _signup(client, "leak_buyer@test.local")
    admin = _admin_client()
    _fund(admin, "leak_buyer@test.local", "1000")
    buyer_key = _issue_key(client)

    assert _call_with_prompt(client, buyer_key, prompt_id).status_code == 200
    assert _balance("leak_author@test.local") == Decimal("50.0000")  # половина от 100
    assert _balance("leak_buyer@test.local") < Decimal("900.5000")  # списаны 100 + токены


def test_child_cannot_publish_a_prompt_through_the_interface(client):
    _signup(client, "leak_pubparent@test.local")
    client.post(
        "/children/new",
        data={"email": "leak_pubkid@test.local", "name": "Ребёнок", "password": "KidPass123"},
        follow_redirects=True,
    )
    kid = _new_client()
    kid.post("/login", data={"email": "leak_pubkid@test.local", "password": "KidPass123"})
    r = kid.post(
        "/prompts",
        data={"title": "Мой", "description": "", "system_prompt": "Ты бот", "price_rub": "100"},
    )
    assert r.status_code == 403


def test_royalty_is_not_paid_out_of_money_that_was_not_collected(client):
    """Если плата уводит баланс в минус, значит денег не было — раздавать
    автору половину того, чего не получили, нельзя."""
    author_client = _new_client()
    _signup(author_client, "leak_author2@test.local")
    author = _customer("leak_author2@test.local")
    prompt_id = _make_prompt(author.id, "500.00", "Дорогой промпт")

    _signup(client, "leak_poor@test.local")
    poor = _customer("leak_poor@test.local")

    async def _run():
        async with SessionLocal() as session:
            prompt = await session.get(Prompt, prompt_id)
            await billing.charge_prompt_fee(session, poor.id, prompt, None)

    asyncio.run(_run())

    assert _balance("leak_poor@test.local") == Decimal("-500.0000")  # долг зафиксирован
    assert _balance("leak_author2@test.local") == Decimal("0.0000")  # но выплаты не было


# ---------- 2. двойная трата через магазин ----------


def _product_id():
    async def _get():
        async with SessionLocal() as session:
            return (await session.execute(select(Product))).scalars().first().id

    return asyncio.run(_get())


def _reserve(payer_id, amount):
    """Имитируем выполняющийся вызов, который держит резерв."""

    async def _create():
        async with SessionLocal() as session:
            await billing.start_call(
                session,
                payer_id,
                payer_id,
                "openai",
                "gpt-5-mini",
                estimated_reserve_rub=Decimal(amount),
            )

    asyncio.run(_create())


def test_shop_purchase_respects_active_reservations(client):
    _signup(client, "leak_shop1@test.local")
    admin = _admin_client()
    target = _fund(admin, "leak_shop1@test.local", "3000")
    product = _product_id()

    _reserve(target.id, "2000")  # идёт долгий вызов, держит 2000 из 3000

    r = client.post(
        "/shop/order",
        data={"product_id": product, "account_email": "x@test.local", "note": ""},
        follow_redirects=True,
    )
    # Товар стоит 2400 — свободно только 1000, покупка должна отбиться
    assert "недостаточно" in r.text.lower() or r.status_code == 402
    assert _balance("leak_shop1@test.local") == Decimal("3000.0000")


def test_shop_purchase_works_when_nothing_is_reserved(client):
    _signup(client, "leak_shop2@test.local")
    admin = _admin_client()
    _fund(admin, "leak_shop2@test.local", "3000")
    product = _product_id()

    r = client.post(
        "/shop/order",
        data={"product_id": product, "account_email": "x@test.local", "note": ""},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert _balance("leak_shop2@test.local") == Decimal("600.0000")  # 3000 - 2400


def test_reservation_and_purchase_cannot_spend_the_same_rubles(client):
    """Совокупная проверка: резерв плюс покупка не могут превысить баланс."""
    _signup(client, "leak_shop3@test.local")
    admin = _admin_client()
    target = _fund(admin, "leak_shop3@test.local", "2500")
    product = _product_id()

    _reserve(target.id, "500")

    client.post(
        "/shop/order",
        data={"product_id": product, "account_email": "x@test.local", "note": ""},
        follow_redirects=True,
    )
    # 2500 - 500 резерва = 2000 свободно, товар за 2400 не помещается
    assert _balance("leak_shop3@test.local") == Decimal("2500.0000")


def test_available_balance_helper_subtracts_only_pending_reservations(client):
    _signup(client, "leak_avail@test.local")
    admin = _admin_client()
    target = _fund(admin, "leak_avail@test.local", "1000")
    _reserve(target.id, "300")

    async def _check():
        async with SessionLocal() as session:
            payer = await session.get(Customer, target.id)
            return await billing.available_balance(session, target.id, payer.balance_rub)

    assert asyncio.run(_check()) == Decimal("700.0000")
