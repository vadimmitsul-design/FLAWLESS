"""Дашборд сроков: порядок, счётчики, человек в строке.

Страница отвечает на один вопрос администратора — «у кого когда кончается
подписка». Главный дефект прежней версии был не в оформлении, а в ПОРЯДКЕ:
сортировка по дате возрастанию поднимала наверх подписку, просроченную
восемь месяцев назад и всеми брошенную, над прокси, истёкшим вчера. Чем
дольше строка гнила, тем выше лезла.

Префикс адресов — `dash_`: тестовая база одна на весь прогон.
"""

import asyncio
from datetime import timedelta

from app.db import SessionLocal
from app.db.models import Customer, Resource, days_phrase, utcnow
from app.services.resources import resource_order, resource_state

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


def _admin_client():
    from fastapi.testclient import TestClient

    from app.main import app

    admin = TestClient(app)
    assert admin.post(
        "/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    ).status_code in (200, 303)
    return admin


def _person(email, name, job_title=None, department=None):
    async def _add():
        async with SessionLocal() as session:
            person = Customer(
                email=email,
                name=name,
                password_hash="x",
                job_title=job_title,
                department=department,
            )
            session.add(person)
            await session.commit()
            return person.id

    return asyncio.run(_add())


def _resource(name, owner_id, days_from_now):
    async def _add():
        async with SessionLocal() as session:
            session.add(
                Resource(
                    kind="subscription",
                    name=name,
                    owner_customer_id=owner_id,
                    expires_at=None
                    if days_from_now is None
                    else utcnow() + timedelta(days=days_from_now),
                )
            )
            await session.commit()

    asyncio.run(_add())


# ---------- слова о сроке ----------


def test_zero_days_is_a_word_not_a_number():
    """Ноль читается как «всё, кончилось», а по days_left сегодня ещё
    оплачено — поэтому словом."""
    assert days_phrase(0) == "истекает сегодня"
    assert days_phrase(1) == "истекает завтра"
    assert days_phrase(5) == "истекает через 5 дн."
    assert days_phrase(-3) == "просрочен на 3 дн."
    assert days_phrase(None) == "срок не указан"


# ---------- порядок ----------


def _ordered(codes_and_days):
    """Список состояний -> порядок после сортировки дашборда."""
    items = []
    for label, code, days in codes_and_days:
        items.append({"r": Resource(name=label), "state": {"code": code, "days": days}})
    items.sort(key=resource_order)
    return [i["r"].name for i in items]


def test_recently_expired_stands_above_long_abandoned():
    """Свежепросроченный чинят, годичный архивируют — значит свежий выше."""
    assert _ordered(
        [
            ("год назад", "expired", -300),
            ("вчера", "expired", -1),
            ("две недели", "expired", -14),
        ]
    ) == ["вчера", "две недели", "год назад"]


def test_buckets_go_burning_first_and_calm_last():
    assert _ordered(
        [
            ("в порядке", "ok", 90),
            ("без срока", "unknown", None),
            ("истекает", "soon", 3),
            ("просрочен", "expired", -2),
        ]
    ) == ["просрочен", "истекает", "без срока", "в порядке"]


def test_inside_soon_the_nearest_is_first():
    assert _ordered([("через 7", "soon", 7), ("сегодня", "soon", 0), ("через 3", "soon", 3)]) == [
        "сегодня",
        "через 3",
        "через 7",
    ]


# ---------- страница ----------


def test_dashboard_shows_who_and_what_position(client):
    """«ФИО, должность» — то, ради чего заводились поля: по строке видно,
    кому продлевать, без похода в другую таблицу."""
    owner = _person("dash_petrov@test.local", "Петров Пётр", "бэкенд-разработчик", "Разработка")
    _resource("Дашборд: подписка Петрова", owner, 3)

    page = _admin_client().get("/admin/resources").text
    assert "Петров Пётр" in page
    assert "бэкенд-разработчик" in page
    assert "Разработка" in page


def test_counters_are_also_filters(client):
    """Счётчик без действия — украшение. Клик по числу должен показывать
    ровно эти строки."""
    owner = _person("dash_filter@test.local", "Фильтров Ф.")
    _resource("Дашборд: просрочено", owner, -5)
    _resource("Дашборд: в порядке", owner, 120)

    admin = _admin_client()
    everything = admin.get("/admin/resources").text
    assert "Дашборд: просрочено" in everything and "Дашборд: в порядке" in everything

    only_expired = admin.get("/admin/resources?f=expired").text
    table = only_expired[only_expired.index('class="rtab"') :]
    assert "Дашборд: просрочено" in table
    assert "Дашборд: в порядке" not in table, "фильтр по состоянию не сработал"


def test_filter_by_owner_answers_who(client):
    """«Покажи всё, что закреплено за Ивановым» — буквальный вопрос
    заказчика."""
    one = _person("dash_one@test.local", "Первый П.")
    two = _person("dash_two@test.local", "Второй В.")
    _resource("Дашборд: первого", one, 30)
    _resource("Дашборд: второго", two, 30)

    page = _admin_client().get(f"/admin/resources?owner={one}").text
    table = page[page.index('class="rtab"') :]
    assert "Дашборд: первого" in table
    assert "Дашборд: второго" not in table


def test_bad_owner_is_an_error_not_a_500(client):
    assert _admin_client().get("/admin/resources?owner=не-число").status_code == 400


def test_resources_without_a_date_are_visible_not_buried(client):
    """Ресурс без срока не попадает в оповещения вовсе — фильтр alerts
    требует expires_at. Если он ещё и тонет в конце списка, он не виден
    нигде."""
    owner = _person("dash_nodate@test.local", "Бессрочный Б.")
    _resource("Дашборд: без даты", owner, None)

    page = _admin_client().get("/admin/resources?f=unknown").text
    table = page[page.index('class="rtab"') :]
    assert "Дашборд: без даты" in table
    assert "без срока" in page, "нет счётчика ресурсов без срока"


def test_summary_line_is_never_empty_when_there_is_something(client):
    owner = _person("dash_line@test.local", "Строкин С.", "тестировщик")
    _resource("Дашборд: для сводки", owner, -1)

    page = _admin_client().get("/admin/resources").text
    head = page[: page.index('class="rtab"')]
    assert "Требует внимания" in head or "Ничего не горит" in head
    assert "просрочен на" in head


def test_state_still_reads_from_the_date_not_from_a_stored_field(client):
    """Опора всего дашборда: статус вычисляется, а не хранится — хранимый
    протухает молча в ту же секунду, как проходит срок."""
    now = utcnow()
    today = Resource(name="x", kind="proxy", expires_at=now)
    assert resource_state(today, now, 7)["code"] == "soon"
    assert (
        resource_state(Resource(name="x", kind="proxy", expires_at=None), now, 7)["code"]
        == "unknown"
    )
