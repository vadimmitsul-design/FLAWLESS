"""core / pages for the Flawless application."""

import re

from app.core.paths import BASE_DIR

# (группа, адрес, заголовок, шаблон, подзаголовок)
DOCS_PAGES = [
    (
        "Начало работы",
        "/docs",
        "Быстрый старт",
        "docs/quickstart.html",
        "От регистрации до первого ответа модели — пять минут и один HTTP-запрос.",
    ),
    (
        "Начало работы",
        "/docs/auth",
        "Аутентификация",
        "docs/auth.html",
        "Как устроены API-ключи, что они ограничивают и как их отозвать.",
    ),
    (
        "Начало работы",
        "/docs/models",
        "Модели и цены",
        "docs/models.html",
        "Какие модели доступны, сколько стоит миллион токенов и что происходит при сбое провайдера.",
    ),
    (
        "API",
        "/docs/chat-completions",
        "Chat Completions",
        "docs/chat_completions.html",
        "Справочник параметров запроса, формата ответа и повторной отправки без двойного списания.",
    ),
    (
        "API",
        "/docs/streaming",
        "Потоковые ответы",
        "docs/streaming.html",
        "Ответ по мере генерации — и что при обрыве соединения происходит с деньгами.",
    ),
    (
        "API",
        "/docs/limits",
        "Ошибки и лимиты",
        "docs/limits.html",
        "Частота запросов, потолки расхода и полный список кодов ошибок.",
    ),
    (
        "Интеграции",
        "/docs/integrations/sdk",
        "SDK и другие клиенты",
        "docs/int_sdk.html",
        "Официальные библиотеки OpenAI, совместимые клиенты — и честный список того, что не подойдёт.",
    ),
    (
        "Интеграции",
        "/docs/integrations/cursor",
        "Cursor",
        "docs/int_cursor.html",
        "Подключение редактора Cursor к моделям через свой ключ и свой адрес.",
    ),
    (
        "Интеграции",
        "/docs/integrations/openwebui",
        "OpenWebUI и LibreChat",
        "docs/int_openwebui.html",
        "Готовый интерфейс чата для команды на своём сервере.",
    ),
    (
        "Оплата и контроль",
        "/docs/billing",
        "Баланс и списания",
        "docs/billing.html",
        "Как рубли на балансе превращаются в вызовы и что сохраняется по каждому из них.",
    ),
    (
        "Переход",
        "/docs/migration",
        "Миграция с OpenAI",
        "docs/migration.html",
        "Что поменять в коде — и чего в сервисе пока нет.",
    ),
]


DOCS_INDEX = {page[1]: i for i, page in enumerate(DOCS_PAGES)}


def _build_docs_search_index() -> list[dict]:
    """Индекс для поиска по документации.

    Заголовки h2 вынимаются из самих шаблонов, а не перечисляются руками:
    иначе каждый новый раздел пришлось бы дублировать ещё и в индексе, и
    поиск тихо отставал бы от документации. Собирается один раз при старте —
    шаблоны на ходу не меняются.
    """
    index: list[dict] = []
    for group, path, title, template, lead in DOCS_PAGES:
        file = BASE_DIR / "templates" / template
        headings: list[str] = []
        if file.exists():
            raw = file.read_text(encoding="utf-8")
            headings = [
                re.sub(r"<[^>]+>", "", h).strip()
                for h in re.findall(r"<h2[^>]*>(.*?)</h2>", raw, flags=re.S)
            ]
        index.append(
            {"path": path, "title": title, "group": group, "lead": lead, "headings": headings}
        )
    return index


def _build_docs_nav() -> list[dict]:
    """Меню собирается из того же списка, что и маршруты: страница не может
    появиться в навигации, не имея обработчика, и наоборот."""
    nav: list[dict] = []
    for group, path, title, _template, _lead in DOCS_PAGES:
        if not nav or nav[-1]["title"] != group:
            nav.append({"title": group, "items": []})
        nav[-1]["items"].append({"path": path, "title": title})
    return nav


DOCS_NAV = _build_docs_nav()


DOCS_SEARCH_INDEX = _build_docs_search_index()


# (адрес, шаблон, заголовок, надзаголовок, H1, подзаголовок, пункт меню)
MARKETING_PAGES = [
    (
        "/models",
        "page_models.html",
        "Модели",
        "Каталог",
        "Три провайдера — один ключ и один баланс",
        "Цены пересчитаны в рубли по действующей наценке и курсу. Тот же прайс, по которому "
        "считается ваш счёт, — расхождения между витриной и списанием быть не может.",
        "models",
    ),
    (
        "/pricing",
        "page_pricing.html",
        "Цены",
        "Цены",
        "Платите за токены, а не за место",
        "Абонентской платы нет. Посчитайте заранее, во сколько обойдётся ваша нагрузка, "
        "и сравните модели между собой.",
        "pricing",
    ),
    (
        "/product/api",
        "page_api.html",
        "API",
        "Продукт",
        "OpenAI-совместимый API с рублёвым биллингом",
        "Тот же формат запроса и ответа, что у OpenAI, — плюс резервные модели, потолки расхода "
        "и защита от двойного списания.",
        "api",
    ),
    (
        "/product/chat",
        "page_chat.html",
        "Чат",
        "Продукт",
        "Веб-чат и бот для тех, кому не нужен код",
        "Те же модели и тот же баланс — через интерфейс в кабинете и через Telegram, "
        "с теми же лимитами, что и в API.",
        "chat",
    ),
    (
        "/solutions/developers",
        "page_sol_developers.html",
        "Разработчикам",
        "Решения",
        "Один ключ вместо трёх аккаунтов и валютной карты",
        "Подключается за минуту к тому, чем вы уже пользуетесь, и показывает себестоимость "
        "каждого вызова.",
        "solutions",
    ),
    (
        "/solutions/agencies",
        "page_sol_agencies.html",
        "Агентствам",
        "Решения",
        "Себестоимость ИИ по каждому проекту",
        "Отдельный ключ на клиента, потолок расхода на проект и выгрузка, которую можно "
        "приложить к акту.",
        "solutions",
    ),
    (
        "/solutions/companies",
        "page_sol_companies.html",
        "Компаниям",
        "Решения",
        "Доступ к моделям для всей команды — под контролем",
        "Единый кошелёк компании, потолки на человека, мгновенный отзыв доступа "
        "и вырезание секретов из запросов.",
        "solutions",
    ),
]


MARKETING_INDEX = {page[0]: page for page in MARKETING_PAGES}
