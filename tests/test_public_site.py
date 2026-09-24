"""Публичные страницы: лендинг с живыми ценами и документация.

Главное, что здесь проверяется, — витрина не врёт. Раньше на лендинге стояли
литералы «[НАЦЕНКА]%» и «[ЮРИДИЧЕСКОЕ НАЗВАНИЕ]»: страница показывала заглушку
вместо настоящих условий. Теперь наценка, курс и цены моделей берутся из той же
таблицы и по той же формуле, что и реальное списание, поэтому тесты сверяют
именно числа, а не наличие блоков.

Числа ищем вместе с соседним символом («31&nbsp;₽», «>31<»), а не как голую
подстроку: в разметке полно чисел в стилях, и «31» само по себе нашлось бы где
угодно — на этом здесь уже обжигались.
"""

import pytest

from app.core.config import settings
from app.core.pages import DOCS_PAGES

# ---------- лендинг ----------


def test_landing_is_public_and_has_no_placeholders(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    for placeholder in ("[НАЦЕНКА]", "[ЮРИДИЧЕСКОЕ НАЗВАНИЕ]", "[КОНТАКТНЫЙ EMAIL]"):
        assert placeholder not in html, f"на лендинге осталась заглушка {placeholder}"


def test_landing_shows_real_markup_and_rate(client):
    """Наценка 30% и курс 95 ₽ приходят из pricing_config (см. conftest).
    На лендинге они стоят в тексте у калькулятора, на /pricing — цифрами."""
    # В шаблоне фраза перенесена по строкам — сравниваем по схлопнутым пробелам.
    landing = " ".join(client.get("/").text.split())
    assert "наценке 30% и курсу 95 ₽" in landing
    pricing = client.get("/pricing").text
    assert "+30%" in pricing
    assert "95&nbsp;₽" in pricing


def test_landing_lists_model_with_ruble_price(client):
    """0.25 USD за 1M входящих × 1.3 наценки × 95 = 30.875 → 31 ₽."""
    html = client.get("/").text
    assert "gpt-5-mini" in html
    assert "31&nbsp;₽" in html
    assert "124&nbsp;₽" in html


def test_landing_marks_model_without_price(client):
    """claude-sonnet и gemini-flash в тестовой базе без прайса — витрина
    обязана сказать об этом, а не показать пустую цену."""
    html = client.get("/").text
    assert "Цена не настроена" in html


def test_landing_uses_configured_api_base_url(client):
    html = client.get("/").text
    assert settings.public_base_url in html


# ---------- документация ----------


def test_docs_index_is_public(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert "Быстрый старт" in r.text


@pytest.mark.parametrize("path", [page[1] for page in DOCS_PAGES])
def test_every_documented_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} не отрендерилась"
    assert "dx-layout" in r.text


def test_unknown_docs_slug_is_404(client):
    assert client.get("/docs/no-such-page").status_code == 404


def test_sidebar_lists_every_page(client):
    """Меню собирается из того же списка, что и маршруты, — проверяем, что
    ни одна страница не потерялась по дороге."""
    html = client.get("/docs").text
    for _group, path, title, _template, _lead in DOCS_PAGES:
        assert f'href="{path}"' in html, f"{path} нет в боковом меню"
        assert title in html


def test_current_page_is_highlighted(client):
    html = client.get("/docs/auth").text
    assert 'class="dx-link is-active" href="/docs/auth"' in html


def test_paging_links_neighbours(client):
    first = client.get("/docs").text
    assert "Дальше" in first
    assert 'href="/docs/auth"' in first
    assert "Назад" not in first

    last = client.get(DOCS_PAGES[-1][1]).text
    assert "Назад" in last
    assert "Дальше" not in last


def test_docs_show_live_prices_and_limits(client):
    """Справочник обязан показывать те же цифры, что и код: потолок токенов и
    частота запросов раньше существовали только в настройках."""
    models = client.get("/docs/models").text
    assert ">31<" in models
    assert ">124<" in models

    limits = client.get("/docs/limits").text
    assert str(settings.rate_limit_per_window) in limits
    assert str(settings.rate_limit_window_seconds) in limits

    reference = client.get("/docs/chat-completions").text
    assert str(settings.max_output_tokens_cap) in reference


def test_docs_do_not_promise_missing_endpoints(client):
    """Миграционная страница должна честно говорить, чего нет: /v1/models в
    сервисе не реализован, и обещать его нельзя."""
    html = client.get("/docs/migration").text
    assert "Чего пока нет" in html
    assert "/v1/embeddings" in html


# ---------- встроенная схема FastAPI ----------


def test_builtin_openapi_schema_is_closed(client):
    """Схема отдавалась анонимно и перечисляла все маршруты, включая
    выключенные флагами разделы. Заодно /docs освободился под нашу
    документацию."""
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/redoc").status_code == 404
