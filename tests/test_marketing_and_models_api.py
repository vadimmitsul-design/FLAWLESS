"""Продуктовые страницы, страницы решений и справочник моделей в API.

Два содержательных требования, ради которых эти тесты и написаны.

Первое: список моделей должен быть ОДИН. Раньше кабинет и веб-чат показывали
сырой реестр из config/models.yaml, а вызов отклонялся, если у модели не было
действующей цены — то есть интерфейс предлагал заведомо ломающийся выбор.
Теперь все места берут список из available_models(), и тест это стережёт.

Второе: сайт не должен обещать несуществующего. Claude Code подключить нельзя —
он ждёт API в формате Anthropic, а шлюз говорит на формате OpenAI, — и упоминаний
о поддержке Claude Code на страницах быть не должно.
"""

import re

import pytest

from app.config import settings
from app.main import _DOCS_PAGES, _MARKETING_PAGES


def _signup(client, email, name="Test User", password="TestPass123"):
    r = client.post(
        "/signup", data={"email": email, "name": name, "password": password}, follow_redirects=True
    )
    assert r.status_code == 200


def _api_key(client):
    r = client.post("/api-key/regenerate", data={"name": "k"})
    return re.search(r"nh_[A-Za-z0-9_-]+", r.text).group(0)


# ---------- /v1/models ----------


def test_models_endpoint_requires_a_key(client):
    assert client.get("/v1/models").status_code == 401


def test_models_endpoint_returns_openai_shape(client):
    _signup(client, "mdl_shape@test.local")
    key = _api_key(client)
    r = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"], "каталог не должен быть пустым"
    for item in body["data"]:
        assert set(item) == {"id", "object", "created", "owned_by"}
        assert item["object"] == "model"
        assert isinstance(item["created"], int)


def test_models_endpoint_hides_models_without_a_price(client):
    """В conftest прайс заведён только для gpt-5-mini. Остальные модели из
    config/models.yaml вызвать нельзя — и предлагать их тоже нельзя."""
    _signup(client, "mdl_priced@test.local")
    key = _api_key(client)
    ids = [m["id"] for m in client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]]
    assert "gpt-5-mini" in ids
    assert "claude-sonnet" not in ids
    assert "gemini-flash" not in ids


def test_single_model_lookup(client):
    _signup(client, "mdl_one@test.local")
    key = _api_key(client)
    h = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/models/gpt-5-mini", headers=h).json()["id"] == "gpt-5-mini"
    assert client.get("/v1/models/claude-sonnet", headers=h).status_code == 404
    assert client.get("/v1/models/no-such-model", headers=h).status_code == 404


def test_model_choice_is_the_same_everywhere(client):
    """Кабинет и веб-чат обязаны предлагать ровно то, что примет вызов.
    Непрайсованная модель в выпадающем списке — это обещание ошибки 503."""
    _signup(client, "mdl_ui@test.local")
    for page in ("/", "/chat"):
        html = client.get(page).text
        assert "gpt-5-mini" in html, f"{page}: рабочая модель пропала из списка"
        assert "claude-sonnet" not in html, f"{page}: предлагается модель без цены"


# ---------- продуктовые страницы и решения ----------


@pytest.mark.parametrize("path", [page[0] for page in _MARKETING_PAGES])
def test_every_marketing_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} не отрендерилась"
    assert "u-foot" in r.text, f"{path}: нет общего подвала"
    assert "u-head" in r.text, f"{path}: нет общей шапки"


def test_unknown_product_and_solution_slugs_are_404(client):
    assert client.get("/product/nope").status_code == 404
    assert client.get("/solutions/nope").status_code == 404


def test_current_section_is_marked_in_the_menu(client):
    assert 'href="/pricing" class="is-here"' in client.get("/pricing").text


def test_pricing_page_carries_the_calculator_with_live_prices(client):
    html = client.get("/pricing").text
    assert 'id="calc-total"' in html
    assert '"rub_in"' in html, "калькулятору не переданы цены"
    assert "+30%" in html
    assert "95&nbsp;₽" in html


def test_models_page_lists_provider_model_ids(client):
    html = client.get("/models").text
    assert "gpt-5-mini" in html
    assert "31&nbsp;₽" in html


# ---------- честность ----------


def test_site_does_not_promise_claude_code(client):
    """Claude Code ждёт API в формате Anthropic (/v1/messages), которого у нас
    нет. Пока метода нет, обещать поддержку нельзя ни на одной странице."""
    pages = ["/", "/product/api", "/solutions/developers", "/docs/integrations/sdk"]
    for path in pages:
        html = client.get(path).text
        if path == "/docs/integrations/sdk":
            assert "Claude Code подключить нельзя" in html
        else:
            assert "Claude Code" not in html, f"{path} обещает Claude Code"


def test_integration_guides_render(client):
    for path in (
        "/docs/integrations/cursor",
        "/docs/integrations/openwebui",
        "/docs/integrations/sdk",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert settings.public_base_url in r.text


def test_integration_guides_are_in_the_docs_menu(client):
    html = client.get("/docs").text
    for _group, path, title, _tpl, _lead in _DOCS_PAGES:
        if path.startswith("/docs/integrations/"):
            assert f'href="{path}"' in html
            assert title in html


# ---------- поиск по документации ----------


def test_search_index_is_built_from_the_templates(client):
    """Заголовки берутся из самих шаблонов: если раздел допишут, а индекс
    забудут — поиск не должен отстать от документации."""
    from app.main import DOCS_SEARCH_INDEX

    assert len(DOCS_SEARCH_INDEX) == len(_DOCS_PAGES)
    by_path = {e["path"]: e for e in DOCS_SEARCH_INDEX}
    assert "Шаг 1. Получить ключ" in by_path["/docs"]["headings"]
    assert "Потолки расхода" in by_path["/docs/limits"]["headings"]
    assert all(e["headings"] for e in DOCS_SEARCH_INDEX), "у страницы не нашлось ни одного h2"


def test_search_is_available_on_every_docs_page(client):
    html = client.get("/docs/auth").text
    assert 'id="dx-open-search"' in html
    assert 'id="dx-index"' in html
    assert "Ctrl K" in html


# ---------- два контура ----------


def test_internal_instance_has_no_public_site(client, monkeypatch):
    """Внутреннему контуру витрина не нужна: сотруднику нечего продавать.
    Выключённая витрина обязана отдавать 404, а не просто прятать ссылки —
    иначе выключение косметическое."""
    monkeypatch.setattr(settings, "enable_public_site", False)
    for path in ("/models", "/pricing", "/product/api", "/solutions/agencies"):
        assert client.get(path).status_code == 404, f"{path} доступен на внутреннем контуре"


def test_internal_instance_sends_anonymous_visitor_to_login(client, monkeypatch):
    monkeypatch.setattr(settings, "enable_public_site", False)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_docs_stay_available_without_the_public_site(client, monkeypatch):
    """Документация нужна и своим разработчикам — её флаг витрины не трогает."""
    monkeypatch.setattr(settings, "enable_public_site", False)
    assert client.get("/docs").status_code == 200
    assert client.get("/docs/chat-completions").status_code == 200


# ---------- закупка через OpenRouter ----------


def test_vendor_is_the_model_maker_not_the_reseller(client):
    """Через OpenRouter поставщик у всех моделей один. Клиент выбирает
    модель, а не канал закупки, поэтому на витрине должен стоять
    производитель — OpenAI, а не OpenRouter."""
    html = client.get("/models").text
    assert "OpenAI" in html
    assert "OPENROUTER" not in html.upper().replace("OPENROUTER_API_KEY", "")


def test_models_endpoint_reports_the_maker_in_owned_by(client):
    _signup(client, "mdl_owner@test.local")
    key = _api_key(client)
    data = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]
    row = next(m for m in data if m["id"] == "gpt-5-mini")
    assert row["owned_by"] == "openai", "owned_by должен называть производителя модели"


def test_alias_is_stable_while_the_route_changes(client):
    """Клиент шлёт алиас, а не идентификатор провайдера: переезд закупки на
    OpenRouter не должен ломать чужой код."""
    from app import llm

    provider, model = llm.resolve_alias("gpt-5-mini")
    assert provider == "openrouter"
    assert model == "openai/gpt-5-mini"


# ---------- шапка кабинета ----------


def _admin_client():
    from fastapi.testclient import TestClient
    from app.main import app

    admin = TestClient(app)
    r = admin.post("/login", data={"email": "admin@test.local", "password": "AdminPass123"})
    assert r.status_code in (200, 303)
    return admin


def test_admin_sections_live_in_one_menu_not_in_the_main_row(client):
    """Было тринадцать ссылок в один ряд: у админа строка переполнялась и
    ломалась на две. Администраторские разделы обязаны жить в выпадающем
    меню, иначе шапка снова расползётся при добавлении раздела."""
    html = _admin_client().get("/").text
    row = html.split('<nav class="cab-links">')[1].split("</nav>")[0]
    for path in ("/admin/overview", "/admin/customers", "/admin/pricing", "/admin/api-keys"):
        assert path not in row, f"{path} стоит в общем ряду ссылок вместо меню"
        assert f'href="{path}"' in html, f"{path} пропал из шапки совсем"
    assert '<summary>Админка</summary>' in html


def test_theme_toggle_sits_inside_the_bar(client):
    """Плавающая кнопка темы висела поверх правого края и наезжала на
    «Выйти». В шапке она должна стоять в потоке."""
    assert 'class="theme-toggle in-bar"' in _admin_client().get("/").text


# ---------- диагностика при старте ----------


def test_startup_reports_models_without_a_price(client, caplog):
    """Сервис обязан сказать про непригодную модель при старте, а не молчать
    до первого платного вызова.

    Ровно это и случилось на практике: при переезде закупки на OpenRouter в
    боевой базе остались строки прайса под старые пары, новые цены не нашли,
    список моделей опустел — и в чате открывался пустой выпадающий список.
    """
    import asyncio
    import logging

    from app.main import _report_model_readiness

    with caplog.at_level(logging.INFO, logger="app.main"):
        asyncio.run(_report_model_readiness())

    text = caplog.text
    # В conftest заведена цена только для gpt-5-mini — остальные обязаны
    # попасть в список недоступных.
    assert "без действующей цены" in text
    assert "claude-sonnet" in text
    assert "моделей готово к вызову: 1 из 4" in text


def test_startup_reports_missing_provider_key(client, caplog, monkeypatch):
    """Ключа провайдера нет — значит вызовы упадут на авторизации.
    Об этом тоже надо предупреждать на старте."""
    import asyncio
    import logging

    from app.main import _report_model_readiness

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with caplog.at_level(logging.INFO, logger="app.main"):
        asyncio.run(_report_model_readiness())

    assert "OPENROUTER_API_KEY" in caplog.text
