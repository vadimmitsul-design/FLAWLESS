"""Ресурсы со сроком: прокси и подписки, закреплённые за сотрудником.

Решение заказчика (2026-09-13): прокси закреплены за человеком, а деньги идут
МИМО рублёвого кошелька — администратор платит картой у поставщика и фиксирует
факт. Поэтому здесь журнал расходов, а не биллинг: wallet_ledger не
затрагивается вовсе, и тесты это стерегут.

Второе требование, ради которого написана половина файла: срок не должен
кончаться молча. Статус считается из даты (а не хранится рядом, где протух бы
в ту же секунду), продление добавляет запись в журнал (а не переписывает
прошлую, иначе история расходов теряется), и об истечении предупреждают
заранее.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import (
    Customer,
    Resource,
    ResourcePayment,
    ResourceRequest,
    WalletLedger,
    as_utc,
)

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


def _customer(email):
    async def _get():
        async with SessionLocal() as session:
            return (
                await session.execute(select(Customer).where(Customer.email == email))
            ).scalar_one()

    return asyncio.run(_get())


def _day(offset_days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _make(admin, owner_id, name, *, expires_in=30, kind="proxy", provider="Прокси-контора"):
    r = admin.post(
        "/admin/resources/new",
        data={
            "kind": kind,
            "name": name,
            "provider": provider,
            "owner_customer_id": owner_id,
            "account": "acc-77",
            "url": "https://example.test/panel",
            "expires_at": _day(expires_in) if expires_in is not None else "",
            "note": "",
        },
    )
    assert r.status_code in (200, 303), r.text

    async def _find():
        async with SessionLocal() as session:
            return (
                await session.execute(select(Resource).where(Resource.name == name))
            ).scalar_one()

    return asyncio.run(_find())


# ---------- кто что видит ----------


def test_employee_sees_only_own_resources(client):
    _signup(client, "res_mine@test.local", name="Свой")
    _signup(client, "res_other@test.local", name="Чужой")
    mine, other = _customer("res_mine@test.local"), _customer("res_other@test.local")

    admin = _admin_client()
    _make(admin, mine.id, "Прокси мой")
    _make(admin, other.id, "Прокси чужой")

    client.post("/login", data={"email": "res_mine@test.local", "password": "TestPass123"})
    page = client.get("/resources")
    assert page.status_code == 200
    assert "Прокси мой" in page.text
    assert "Прокси чужой" not in page.text, "сотрудник видит чужой ресурс"


def test_admin_sees_everything(client):
    _signup(client, "res_all@test.local")
    owner = _customer("res_all@test.local")
    admin = _admin_client()
    _make(admin, owner.id, "Прокси для админского списка")
    assert "Прокси для админского списка" in admin.get("/admin/resources").text


def test_non_admin_cannot_reach_admin_page(client):
    _signup(client, "res_notadmin@test.local")
    assert client.get("/admin/resources").status_code == 403


# ---------- срок и статус ----------


def test_status_is_computed_from_the_date(client):
    """Статус не хранится: хранимый протухает молча в ту же секунду, как
    проходит срок."""
    _signup(client, "res_status@test.local")
    owner = _customer("res_status@test.local")
    admin = _admin_client()
    _make(admin, owner.id, "Просроченный прокси", expires_in=-3)
    _make(admin, owner.id, "Истекающий прокси", expires_in=2)
    _make(admin, owner.id, "Спокойный прокси", expires_in=200)

    client.post("/login", data={"email": "res_status@test.local", "password": "TestPass123"})
    page = client.get("/resources").text
    assert "Просрочен" in page
    assert "Истекает" in page
    assert "Активен" in page


def test_resource_without_a_date_does_not_pretend_to_be_fine(client):
    _signup(client, "res_nodate@test.local")
    owner = _customer("res_nodate@test.local")
    admin = _admin_client()
    _make(admin, owner.id, "Прокси без срока", expires_in=None)

    client.post("/login", data={"email": "res_nodate@test.local", "password": "TestPass123"})
    assert "Срок не указан" in client.get("/resources").text


# ---------- журнал платежей ----------


def test_payment_extends_the_term_and_lands_in_the_journal(client):
    _signup(client, "res_pay@test.local")
    owner = _customer("res_pay@test.local")
    admin = _admin_client()
    resource = _make(admin, owner.id, "Прокси для продления", expires_in=5)

    r = admin.post(
        f"/admin/resources/{resource.id}/pay",
        data={"amount": "1500.00", "currency": "RUB", "paid_at": _day(0),
              "period_end": _day(65), "note": "два месяца"},
    )
    assert r.status_code in (200, 303)

    async def _check():
        async with SessionLocal() as session:
            fresh = await session.get(Resource, resource.id)
            pays = (
                await session.execute(
                    select(ResourcePayment).where(ResourcePayment.resource_id == resource.id)
                )
            ).scalars().all()
            return as_utc(fresh.expires_at), pays

    expires_at, pays = asyncio.run(_check())
    assert len(pays) == 1
    assert pays[0].amount == Decimal("1500.00")
    assert (expires_at - datetime.now(timezone.utc)).days >= 60, "срок не продлился"
    # Период платежа начинается там, где кончался прежний срок — не с нуля.
    assert pays[0].period_start is not None


def test_backdated_payment_does_not_shorten_the_paid_term(client):
    """Запись задним числом не должна «укорачивать» уже оплаченный период —
    иначе одна опечатка в дате делает рабочий прокси просроченным."""
    _signup(client, "res_back@test.local")
    owner = _customer("res_back@test.local")
    admin = _admin_client()
    resource = _make(admin, owner.id, "Прокси с длинным сроком", expires_in=120)

    admin.post(
        f"/admin/resources/{resource.id}/pay",
        data={"amount": "100", "currency": "RUB", "paid_at": _day(-30), "period_end": _day(10)},
    )

    async def _left():
        async with SessionLocal() as session:
            fresh = await session.get(Resource, resource.id)
            return (as_utc(fresh.expires_at) - datetime.now(timezone.utc)).days

    assert asyncio.run(_left()) >= 115, "срок уехал назад из-за записи задним числом"


def test_payments_never_touch_the_wallet(client):
    """Деньги за прокси идут мимо рублёвого кошелька: админ платит картой.
    Если это когда-нибудь начнёт двигать баланс, отчёты по выручке поедут."""
    _signup(client, "res_wallet@test.local")
    owner = _customer("res_wallet@test.local")
    admin = _admin_client()
    resource = _make(admin, owner.id, "Прокси без кошелька")

    async def _ledger_count():
        async with SessionLocal() as session:
            return len(
                (
                    await session.execute(
                        select(WalletLedger).where(WalletLedger.customer_id == owner.id)
                    )
                ).scalars().all()
            )

    before = asyncio.run(_ledger_count())
    admin.post(
        f"/admin/resources/{resource.id}/pay",
        data={"amount": "999", "currency": "RUB", "paid_at": _day(0), "period_end": _day(30)},
    )
    assert asyncio.run(_ledger_count()) == before, "оплата ресурса тронула кошелёк"

    async def _balance():
        async with SessionLocal() as session:
            return (await session.get(Customer, owner.id)).balance_rub

    assert asyncio.run(_balance()) == Decimal("0.0000")


def test_archive_keeps_the_payment_history(client):
    """Архив вместо удаления: на ресурс ссылается журнал, и история расходов
    компании не должна исчезать вместе с отменённой подпиской."""
    _signup(client, "res_arch@test.local")
    owner = _customer("res_arch@test.local")
    admin = _admin_client()
    resource = _make(admin, owner.id, "Прокси в архив")
    admin.post(
        f"/admin/resources/{resource.id}/pay",
        data={"amount": "250", "currency": "RUB", "paid_at": _day(0), "period_end": _day(30)},
    )
    admin.post(f"/admin/resources/{resource.id}/archive")

    async def _state():
        async with SessionLocal() as session:
            fresh = await session.get(Resource, resource.id)
            pays = (
                await session.execute(
                    select(ResourcePayment).where(ResourcePayment.resource_id == resource.id)
                )
            ).scalars().all()
            return fresh.archived, len(pays)

    archived, pay_count = asyncio.run(_state())
    assert archived is True
    assert pay_count == 1, "история платежей пропала вместе с архивацией"
    assert "Прокси в архив" not in admin.get("/admin/resources?show=active").text
    assert "Прокси в архив" in admin.get("/admin/resources?show=all").text


# ---------- проверки ввода ----------


def test_bad_input_is_rejected(client):
    _signup(client, "res_bad@test.local")
    owner = _customer("res_bad@test.local")
    admin = _admin_client()
    resource = _make(admin, owner.id, "Прокси для проверок")

    assert admin.post("/admin/resources/new", data={
        "kind": "nonsense", "name": "x", "owner_customer_id": owner.id,
    }).status_code == 400
    assert admin.post(f"/admin/resources/{resource.id}/pay", data={
        "amount": "-5", "currency": "RUB", "period_end": _day(30),
    }).status_code == 400
    assert admin.post(f"/admin/resources/{resource.id}/pay", data={
        "amount": "10", "currency": "GBP", "period_end": _day(30),
    }).status_code == 400
    assert admin.post(f"/admin/resources/{resource.id}/pay", data={
        "amount": "10", "currency": "RUB", "period_end": "позавчера",
    }).status_code == 400


# ---------- предупреждение ----------


def test_expiring_resource_produces_an_alert(client):
    """Срок не должен кончаться молча."""
    from app import alerts

    _signup(client, "res_alert@test.local", name="Петров")
    owner = _customer("res_alert@test.local")
    admin = _admin_client()
    _make(admin, owner.id, "Прокси на исходе", expires_in=2)

    async def _collect():
        async with SessionLocal() as session:
            return await alerts._collect_problems(session)

    problems = asyncio.run(_collect())
    texts = " ".join(text for _key, text in problems)
    assert "Прокси на исходе" in texts
    assert "Петров" in texts, "в предупреждении не видно, за кем закреплён ресурс"


def test_alert_key_changes_after_renewal(client):
    """Ключ антиспама включает дату окончания: продлили — предупредят заново,
    а не промолчат, решив, что уже говорили."""
    from app import alerts

    _signup(client, "res_alert2@test.local")
    owner = _customer("res_alert2@test.local")
    admin = _admin_client()
    resource = _make(admin, owner.id, "Прокси с продлением", expires_in=1)

    async def _keys():
        async with SessionLocal() as session:
            return [k for k, _t in await alerts._collect_problems(session)]

    before = [k for k in asyncio.run(_keys()) if k.startswith(f"resource:{resource.id}:")]
    admin.post(
        f"/admin/resources/{resource.id}/pay",
        data={"amount": "100", "currency": "RUB", "paid_at": _day(0), "period_end": _day(3)},
    )
    after = [k for k in asyncio.run(_keys()) if k.startswith(f"resource:{resource.id}:")]
    assert before and after and before[0] != after[0]


# ---------- два контура ----------


def test_section_is_off_for_the_client_instance(client, monkeypatch):
    """Клиентам продают доступ к моделям; чужие прокси им ни к чему.
    Выключённый раздел обязан отдавать 404, а не просто прятать ссылку."""
    _signup(client, "res_flag@test.local")
    monkeypatch.setattr(settings, "enable_resources", False)
    assert client.get("/resources").status_code == 404
    assert _admin_client().get("/admin/resources").status_code == 404


def test_section_disappears_from_the_menu_when_off(client, monkeypatch):
    _signup(client, "res_flag2@test.local")
    assert "/resources" in client.get("/").text
    monkeypatch.setattr(settings, "enable_resources", False)
    assert "/resources" not in client.get("/").text


# ---------- цикл заявки: сотрудник просит, администратор оплачивает ----------


def _login(client, email, password="TestPass123"):
    r = client.post("/login", data={"email": email, "password": password})
    assert r.status_code in (200, 303)


def _ask(client, name, **over):
    data = {
        "kind": "subscription",
        "name": name,
        "provider": "OpenAI",
        "account": "user@company.test",
        "period_months": "1",
        "estimated_amount": "2000",
        "reason": "нужно для работы",
    }
    data.update(over)
    r = client.post("/resources/request", data=data)
    assert r.status_code in (200, 303), r.text

    async def _find():
        async with SessionLocal() as session:
            return (
                await session.execute(
                    select(ResourceRequest).where(ResourceRequest.name == name)
                )
            ).scalar_one()

    return asyncio.run(_find())


def test_employee_request_reaches_the_admin_queue(client):
    """Смысл заявки в том, что администратор не угадывает, кому что нужно."""
    _signup(client, "req_new@test.local", name="Сидоров")
    _login(client, "req_new@test.local")
    _ask(client, "Подписка для Сидорова")

    assert "Подписка для Сидорова" in client.get("/resources").text
    admin_page = _admin_client().get("/admin/resources").text
    assert "Подписка для Сидорова" in admin_page
    assert "Сидоров" in admin_page, "не видно, кто просит"


def test_employee_sees_only_own_requests(client):
    _signup(client, "req_a@test.local")
    _signup(client, "req_b@test.local")
    _login(client, "req_a@test.local")
    _ask(client, "Заявка первого")
    _login(client, "req_b@test.local")
    _ask(client, "Заявка второго")

    page = client.get("/resources").text
    assert "Заявка второго" in page
    assert "Заявка первого" not in page, "видна чужая заявка"


def test_fulfilling_creates_a_resource_for_the_requester(client):
    """Оплатили — заявка превращается в ресурс со сроком, владелец тот, кто
    просил, а не администратор, который платил."""
    _signup(client, "req_ok@test.local", name="Иванов")
    owner = _customer("req_ok@test.local")
    _login(client, "req_ok@test.local")
    req = _ask(client, "ChatGPT Plus для Иванова")

    admin = _admin_client()
    r = admin.post(
        f"/admin/resource-requests/{req.id}/fulfil",
        data={"amount": "2400", "currency": "RUB", "period_end": _day(30),
              "url": "https://chat.openai.test", "decision_note": "оплатил картой"},
    )
    assert r.status_code in (200, 303)

    async def _state():
        async with SessionLocal() as session:
            fresh = await session.get(ResourceRequest, req.id)
            res = await session.get(Resource, fresh.resource_id) if fresh.resource_id else None
            pays = (
                await session.execute(
                    select(ResourcePayment).where(ResourcePayment.resource_id == fresh.resource_id)
                )
            ).scalars().all()
            return fresh.status, res, pays

    status, resource, pays = asyncio.run(_state())
    assert status == "fulfilled"
    assert resource is not None, "ресурс не создан"
    assert resource.owner_customer_id == owner.id, "ресурс достался не тому, кто просил"
    assert resource.name == "ChatGPT Plus для Иванова"
    assert resource.account == "user@company.test", "аккаунт из заявки потерян"
    assert len(pays) == 1 and pays[0].amount == Decimal("2400.00")

    # и сотрудник видит его у себя со сроком
    _login(client, "req_ok@test.local")
    mine = client.get("/resources").text
    assert "ChatGPT Plus для Иванова" in mine
    assert "оплачена" in mine


def test_rejection_keeps_the_reason_visible_to_the_employee(client):
    """Отказ без причины заставляет подавать то же самое снова."""
    _signup(client, "req_no@test.local")
    _login(client, "req_no@test.local")
    req = _ask(client, "Ненужная подписка")

    admin = _admin_client()
    admin.post(
        f"/admin/resource-requests/{req.id}/reject",
        data={"decision_note": "есть общий корпоративный доступ"},
    )

    _login(client, "req_no@test.local")
    page = client.get("/resources").text
    assert "отклонена" in page
    assert "есть общий корпоративный доступ" in page

    async def _no_resource():
        async with SessionLocal() as session:
            fresh = await session.get(ResourceRequest, req.id)
            return fresh.resource_id

    assert asyncio.run(_no_resource()) is None, "отклонённая заявка создала ресурс"


def test_a_decided_request_cannot_be_decided_again(client):
    """Иначе двойной клик по «Оплачено» заводит два ресурса и два платежа."""
    _signup(client, "req_twice@test.local")
    _login(client, "req_twice@test.local")
    req = _ask(client, "Подписка для двойного клика")

    admin = _admin_client()
    first = admin.post(
        f"/admin/resource-requests/{req.id}/fulfil",
        data={"amount": "100", "currency": "RUB", "period_end": _day(30)},
    )
    assert first.status_code in (200, 303)
    second = admin.post(
        f"/admin/resource-requests/{req.id}/fulfil",
        data={"amount": "100", "currency": "RUB", "period_end": _day(30)},
    )
    assert second.status_code == 409
    assert admin.post(
        f"/admin/resource-requests/{req.id}/reject", data={"decision_note": "поздно"}
    ).status_code == 409

    async def _count():
        async with SessionLocal() as session:
            return len(
                (
                    await session.execute(
                        select(Resource).where(Resource.name == "Подписка для двойного клика")
                    )
                ).scalars().all()
            )

    assert asyncio.run(_count()) == 1, "двойное решение завело второй ресурс"


def test_request_input_is_validated(client):
    _signup(client, "req_bad@test.local")
    _login(client, "req_bad@test.local")
    assert client.post("/resources/request", data={"kind": "nonsense", "name": "x"}).status_code == 400
    assert client.post("/resources/request", data={"kind": "proxy", "name": "  "}).status_code == 400
    assert client.post("/resources/request", data={
        "kind": "proxy", "name": "ok", "period_months": "0"}).status_code == 400
    assert client.post("/resources/request", data={
        "kind": "proxy", "name": "ok", "estimated_amount": "-5"}).status_code == 400
    assert client.post("/resources/request", data={
        "kind": "proxy", "name": "ok", "estimated_amount": "вагон"}).status_code == 400


def test_requests_are_off_with_the_section(client, monkeypatch):
    _signup(client, "req_flag@test.local")
    _login(client, "req_flag@test.local")
    monkeypatch.setattr(settings, "enable_resources", False)
    assert client.post("/resources/request", data={"kind": "proxy", "name": "x"}).status_code == 404
