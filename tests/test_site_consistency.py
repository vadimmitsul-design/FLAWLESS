"""Витрина как один сайт: одна шапка и тема, которая не меняется сама.

Заказчик поймал это глазами: переход с лендинга в документацию терял половину
меню и переключал страницу со светлого на тёмное. Причина — у документации
была своя шапка, написанная отдельно от общей. Две шапки не могут не
разъехаться, поэтому тесты стерегут не внешний вид, а само это свойство:
шапка одна, и умолчание темы у витрины и у справочника совпадает.
"""

import pytest

from app.core.config import settings
from app.core.pages import MARKETING_PAGES

# Все пункты меню витрины. Пропажа любого — это и есть тот баг.
MENU = ["/models", "/pricing", "/product/api", "/product/chat", "/solutions/developers", "/docs"]

PUBLIC_PAGES = ["/", "/docs", "/docs/billing"] + [p[0] for p in MARKETING_PAGES]


def _signup(client, email, name="Site Tester", password="TestPass123"):
    r = client.post(
        "/signup", data={"email": email, "name": name, "password": password}, follow_redirects=True
    )
    assert r.status_code == 200, r.text[:200]


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_every_public_page_carries_the_same_menu(client, path):
    html = client.get(path).text
    for item in MENU:
        assert f'href="{item}"' in html, f"на {path} пропал пункт меню {item}"


def test_docs_no_longer_have_a_header_of_their_own(client):
    """Своя шапка — это две разметки, которые обязаны совпадать вручную.
    Именно так и потерялись API, Чат и Решения."""
    html = client.get("/docs").text
    assert "dx-top" not in html, "у документации снова своя шапка"
    assert 'class="u-head"' in html, "документация не взяла общую шапку"


def test_docs_highlight_their_own_menu_item(client):
    html = client.get("/docs").text
    assert '<a href="/docs" class="is-here"' in html, "пункт «Документация» не подсвечен"


def test_docs_keep_their_search(client):
    """Поиск живёт в общей шапке под условием и не должен потеряться при
    переезде."""
    assert 'id="dx-open-search"' in client.get("/docs").text
    assert 'id="dx-open-search"' not in client.get("/pricing").text, (
        "кнопка поиска по документации вылезла на страницу, где искать нечего"
    )


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_showroom_and_docs_open_in_the_same_theme(client, path):
    """Витрина всегда тёмная. Если документация открывается светлой, переход
    выглядит как самопроизвольная смена темы — именно на это и пожаловались."""
    assert 'data-theme-default="dark"' in client.get(path).text, (
        f"{path} открывается не в теме витрины"
    )


def test_the_users_own_choice_is_not_overwritten(client):
    """Умолчание ставится ТОЛЬКО когда в localStorage ничего нет: иначе
    переключатель темы перестал бы работать на второй же странице."""
    html = client.get("/docs").text
    script = html[html.index("<script>") : html.index("</script>")]
    assert "localStorage.getItem('theme')" in script
    assert script.index("localStorage.getItem('theme')") < script.index("data-theme-default"), (
        "умолчание страницы читается раньше выбора пользователя"
    )


def test_internal_instance_docs_follow_the_cabinet_theme(client, monkeypatch):
    """Во внутреннем контуре витрины нет вовсе: документация там — часть
    кабинета, и навязывать ей тёмное значило бы устроить ту же самую
    самопроизвольную смену темы, только на переходе из кабинета."""
    monkeypatch.setattr(settings, "enable_public_site", False)
    assert 'data-theme-default="dark"' not in client.get("/docs").text


def test_guest_is_offered_to_sign_up(client):
    # Искать ">В кабинет<", а не голую фразу: она встречается и в комментарии
    # внутри CSS, и тест прошёл бы мимо разметки. В этом проекте на таком
    # совпадении уже обжигались дважды.
    html = client.get("/docs").text
    assert 'href="/signup"' in html
    assert ">В кабинет<" not in html, "гостю предлагают кабинет, которого у него нет"


def test_signed_in_visitor_is_offered_the_cabinet(client):
    """Вошедшему «Начать» бессмысленно — он уже начал."""
    _signup(client, "sitecons_in@test.local")
    html = client.get("/docs").text
    assert ">В кабинет<" in html
    assert 'href="/signup"' not in html, "вошедшему предлагают зарегистрироваться заново"


def test_public_pages_do_not_render_the_cabinet_header(client):
    """Признак входа передаётся отдельным флагом, а не объектом customer:
    по customer шаблон base.html рисует ШАПКУ КАБИНЕТА, и на странице
    оказалось бы две шапки подряд."""
    _signup(client, "sitecons_two@test.local")
    html = client.get("/docs").text
    assert 'class="cab"' not in html, "на публичной странице появилась шапка кабинета"
    assert html.count('class="u-head"') == 1


# ---------- каталог моделей доезжает до разметки ----------


@pytest.mark.parametrize("path", ["/", "/pricing"])
def test_calculator_gets_the_real_catalog(client, path):
    """Калькулятор считает по ценам из БД. Пустой список моделей — это не
    «некрасиво», а страница, которая молча перестала считать: выпадающий
    список пуст, сумма не выводится. Отдаётся при этом честный 200."""
    html = client.get(path).text
    assert '<script id="calc-data" type="application/json">[]' not in html, (
        "калькулятор собран с пустым списком моделей"
    )
    assert '<option value="0">' in html, "в выборе модели нет ни одного пункта"
