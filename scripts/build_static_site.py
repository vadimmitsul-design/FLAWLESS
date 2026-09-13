# -*- coding: utf-8 -*-
"""Публичная витрина в статику: папка, которую принимает Netlify.

Витрина — лендинг, страницы продукта и решений, документация — это чтение
без единой формы: она ничего не знает про конкретного посетителя. Значит её
можно отдать с CDN, а сервер оставить тому, чему без него нельзя: кабинету,
чату, биллингу и API.

Страницы берутся ИЗ ТЕХ ЖЕ шаблонов, что рендерит сервер: приложение
поднимается в памяти (ASGI, без сети и без порта) и опрашивается как обычный
сайт. Копировать вёрстку в отдельный статический проект нельзя — две копии
разъезжаются на первой же правке, и витрина начинает обещать не то, что
делает сервис.

Ссылки: /login, /signup, /chat статикой отдать невозможно, поэтому в
собранных страницах они переписываются на адрес backend-а (--app-url), и туда
же ведут редиректы Netlify — для ссылок, сохранённых у людей в закладках.

Запуск (из корня проекта):
    .venv/Scripts/python scripts/build_static_site.py
        --app-url https://app.flawless.ru --site-url https://flawless.ru

Цены по умолчанию берутся из временной SQLite, засеянной scripts/seed_prices.py
(тот же прайс, что в проде). Чтобы собрать витрину с ценами из боевой базы:
    --database-url postgresql+asyncpg://neurohub:...@localhost:5434/neurohub

Внутри контейнера временная SQLite недоступна (aiosqlite стоит только в
requirements-dev.txt), поэтому там --database-url обязателен.
"""

import argparse
import asyncio
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Разделы, которым нужен сервер. Ссылки на них внутри страниц переписываются
# автоматически (всё, чего нет среди собранных страниц), а этот список нужен
# только для редиректов Netlify: чтобы сохранённая в закладках ссылка вида
# flawless.ru/login не упиралась в 404, а уводила в приложение.
# /v1 здесь сознательно нет: API живёт на своём домене (PUBLIC_BASE_URL), и
# редирект POST-запроса — источник трудноуловимых ошибок у клиентов.
APP_ROUTES = [
    "/login", "/logout", "/signup", "/forgot-password", "/verify",
    "/chat", "/keys", "/topup", "/resources", "/shop", "/prompts",
    "/archive", "/children", "/admin", "/telegram",
]

SITE_DESCRIPTION = (
    "Доступ к GPT, Claude и Gemini по одному ключу и одному рублёвому балансу. "
    "OpenAI-совместимый API, оплата в рублях, себестоимость каждого вызова видна."
)


# ---------- окружение ----------

def _prepare_env(database_url: str, api_url: str | None) -> None:
    """Переменные ставятся ДО импорта app.*: Settings() читается один раз при
    импорте модуля. Переменные окружения приоритетнее .env, поэтому боевая
    база и боевые ключи из .env в сборку не попадают."""
    os.environ["DATABASE_URL"] = database_url
    os.environ["SESSION_SECRET"] = "static-build-secret-not-used-anywhere-0123456789"
    os.environ["ENABLE_PUBLIC_SITE"] = "true"
    os.environ["SIGNUP_MODE"] = "open"
    os.environ["ENVIRONMENT"] = "development"
    # Пустой токен: lifespan умеет ходить в Telegram. При ASGI-опросе lifespan
    # не запускается, но полагаться на это не стоит.
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    if api_url:
        os.environ["PUBLIC_BASE_URL"] = api_url


# ---------- рендер ----------

async def _seed_temporary_db() -> None:
    from app.db import engine
    from app.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sys.path.insert(0, str(ROOT / "scripts"))
    import seed_prices  # тот же прайс, что в проде: расходиться им нельзя

    await seed_prices.main()


def _page_list(app_main) -> list[tuple[str, str]]:
    """(адрес, описание для <meta name=description>)."""
    pages = [("/", SITE_DESCRIPTION)]
    pages += [(path, lead) for path, _tpl, _t, _k, _h1, lead, _here in app_main._MARKETING_PAGES]
    pages += [(path, lead) for _grp, path, _title, _tpl, lead in app_main._DOCS_PAGES]
    return pages


def _out_file(out_dir: Path, path: str) -> Path:
    """Каталог с index.html на каждый адрес: так Netlify отдаёт /pricing без
    .html в адресе и без единого редиректа."""
    if path == "/":
        return out_dir / "index.html"
    return out_dir / path.strip("/") / "index.html"


# ---------- ссылки и метатеги ----------

_LINK_RE = re.compile(r'(href|action)="(/[^"]*)"')


def _rewrite_links(html: str, known: set[str], app_url: str, moved: dict[str, int]) -> str:
    """Всё, чего нет среди собранных страниц, уезжает в приложение.

    Список «серверных» адресов не перечисляется руками специально: забытый в
    нём новый раздел дал бы на витрине ссылку в никуда, а так по умолчанию
    туда уходит любой неизвестный адрес, и каждый такой случай виден в отчёте
    сборки.
    """
    def sub(m: "re.Match[str]") -> str:
        attr, target = m.group(1), m.group(2)
        base = target.split("#")[0].split("?")[0].rstrip("/") or "/"
        if base in known:
            return m.group(0)
        moved[base] = moved.get(base, 0) + 1
        return f'{attr}="{app_url}{target}"'

    return _LINK_RE.sub(sub, html)


def _fix_cabinet_link(html: str, app_url: str) -> str:
    """В шапке документации «В кабинет» ведёт на «/» — на сервере это кабинет
    для вошедшего и лендинг для гостя. В статике «/» всегда лендинг, поэтому
    кнопку нужно увести в приложение явно."""
    return html.replace(
        '<a href="/" class="dx-cabinet">', f'<a href="{app_url}/" class="dx-cabinet">'
    )


def _inject_meta(html: str, canonical: str | None, description: str) -> str:
    """Canonical и Open Graph — то, чего у серверных страниц не было: там
    витрина жила вперемешку с кабинетом, и каноничного адреса у неё не было.

    У страницы 404 канонического адреса нет вовсе: она отдаётся на ЛЮБОЙ
    несуществующий адрес, и canonical склеил бы весь мусор в одну страницу
    в индексе поисковика.
    """
    title_match = re.search(r"<title>(.*?)</title>", html, flags=re.S)
    title = title_match.group(1).strip() if title_match else "Flawless"
    desc = re.sub(r"\s+", " ", description or "").strip().replace('"', "&quot;")
    tags = (
        f'\n<meta name="description" content="{desc}">'
        f'\n<meta property="og:type" content="website">'
        f'\n<meta property="og:site_name" content="Flawless">'
        f'\n<meta property="og:title" content="{title}">'
        f'\n<meta property="og:description" content="{desc}">'
        f'\n<meta name="twitter:card" content="summary_large_image">'
    )
    if canonical:
        tags += (
            f'\n<link rel="canonical" href="{canonical}">'
            f'\n<meta property="og:url" content="{canonical}">'
        )
    return html.replace("</title>", "</title>" + tags, 1)


# ---------- файлы Netlify ----------

def _redirect_rules(app_url: str) -> list[tuple[str, str, int]]:
    rules: list[tuple[str, str, int]] = []
    for route in APP_ROUTES:
        rules.append((route, f"{app_url}{route}", 301))
        rules.append((f"{route}/*", f"{app_url}{route}/:splat", 301))
    return rules


# Заголовки безопасности. CSP нет сознательно: страницы держат стили и
# скрипты инлайном (вёрстка одна на сервер и на витрину), и честный CSP
# потребовал бы либо 'unsafe-inline', либо пересчёта хэшей на каждую правку.
_HEADERS = [
    ("X-Frame-Options", "SAMEORIGIN"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
]


def _write_netlify_files(out_dir: Path, app_url: str, site_url: str, paths: list[str]) -> None:
    rules = _redirect_rules(app_url)

    # _redirects и _headers работают и при перетаскивании папки в браузер, и
    # при деплое из git; netlify.toml — для git-деплоя и настроек сборки.
    # Все три собираются из одних данных, чтобы не разъехались.
    (out_dir / "_redirects").write_text(
        "# Сгенерировано scripts/build_static_site.py — не править руками.\n"
        + "\n".join(f"{src}  {dst}  {code}" for src, dst, code in rules)
        + "\n",
        encoding="utf-8",
    )
    (out_dir / "_headers").write_text(
        "/*\n" + "".join(f"  {name}: {value}\n" for name, value in _HEADERS),
        encoding="utf-8",
    )

    toml = [
        "# Сгенерировано scripts/build_static_site.py — не править руками.",
        "# Папка уже собрана, собирать на стороне Netlify нечего.",
        "[build]",
        '  publish = "."',
        '  command = ""',
        "",
        "[[headers]]",
        '  for = "/*"',
        "  [headers.values]",
    ]
    toml += [f'    {name} = "{value}"' for name, value in _HEADERS]
    toml.append("")
    for src, dst, code in rules:
        toml += [
            "[[redirects]]",
            f'  from = "{src}"',
            f'  to = "{dst}"',
            f"  status = {code}",
            "  force = true",
            "",
        ]
    (out_dir / "netlify.toml").write_text("\n".join(toml), encoding="utf-8")

    (out_dir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {site_url}/sitemap.xml\n", encoding="utf-8"
    )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = "".join(
        f"  <url><loc>{site_url}{p}</loc>"
        f"<lastmod>{today}</lastmod>"
        f"<priority>{'1.0' if p == '/' else '0.7'}</priority></url>\n"
        for p in paths
    )
    (out_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}</urlset>\n",
        encoding="utf-8",
    )


# ---------- сборка ----------

async def _page_404_context(app_main) -> dict:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        ctx = await app_main._public_page_context(session)
    ctx.update(
        {
            "page_title": "Страница не найдена",
            "page_kicker": "404",
            "page_h1": "Такой страницы нет",
            "page_lead": None,
            "page_cta": False,
            "here": None,
        }
    )
    return ctx


async def build(out_dir: Path, app_url: str, site_url: str, seed: bool) -> int:
    import httpx

    if seed:
        await _seed_temporary_db()

    from app import main as app_main

    pages = _page_list(app_main)
    known = {p.rstrip("/") or "/" for p, _ in pages}

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    moved: dict[str, int] = {}
    transport = httpx.ASGITransport(app=app_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://build.local") as client:
        for path, description in pages:
            response = await client.get(path)
            if response.status_code != 200:
                print(f"ОШИБКА: {path} отдал {response.status_code}")
                return 1
            html = response.text
            had_cabinet = "dx-cabinet" in html
            html = _fix_cabinet_link(html, app_url)
            if had_cabinet and f'{app_url}/" class="dx-cabinet"' not in html:
                print(f"ОШИБКА: {path} — кнопка «В кабинет» изменилась, ссылка осталась бы на витрине")
                return 1
            html = _rewrite_links(html, known, app_url, moved)
            html = _inject_meta(html, site_url + path, description)
            target = _out_file(out_dir, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(html, encoding="utf-8")
            print(f"  {path:<34} -> {target.relative_to(out_dir).as_posix()}  ({len(html) // 1024} КБ)")

        # 404 у Netlify — обычный файл в корне. Шаблон общий с остальной
        # витриной, поэтому «страница не найдена» выглядит как часть сайта.
        ctx = await _page_404_context(app_main)
        html = app_main.templates.get_template("page_404.html").render(ctx)
        html = _fix_cabinet_link(html, app_url)
        html = _rewrite_links(html, known, app_url, moved)
        html = _inject_meta(html, None, "Страница не найдена.")
        (out_dir / "404.html").write_text(html, encoding="utf-8")
        print(f"  {'404':<34} -> 404.html")

    _write_netlify_files(out_dir, app_url, site_url, [p for p, _ in pages])

    print("\nСсылки, уведённые в приложение:")
    for target, count in sorted(moved.items()):
        print(f"  {app_url}{target}  x{count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", default="dist", help="куда собрать (по умолчанию dist/)")
    parser.add_argument("--app-url", default="https://app.flawless.ru",
                        help="адрес backend-а: туда ведут вход, регистрация и кабинет")
    parser.add_argument("--site-url", default="https://flawless.ru",
                        help="адрес самой витрины: canonical, sitemap, Open Graph")
    parser.add_argument("--api-url", default=None,
                        help="адрес API для примеров в документации (иначе PUBLIC_BASE_URL из .env)")
    parser.add_argument("--database-url", default=None,
                        help="взять цены из этой базы вместо временной с сидом")
    args = parser.parse_args()

    app_url = args.app_url.rstrip("/")
    site_url = args.site_url.rstrip("/")
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir

    os.chdir(ROOT)  # config/models.yaml и .env читаются относительно корня
    sys.path.insert(0, str(ROOT))

    tmp_dir = None
    if args.database_url:
        database_url, seed = args.database_url, False
    else:
        tmp_dir = tempfile.mkdtemp(prefix="flawless-static-")
        database_url = f"sqlite+aiosqlite:///{Path(tmp_dir).as_posix()}/build.db"
        seed = True

    _prepare_env(database_url, args.api_url)

    print(f"Витрина:     {site_url}")
    print(f"Приложение:  {app_url}")
    print(f"Цены из:     {'временной базы с сидом' if seed else args.database_url}")
    print(f"Собираю в:   {out_dir}\n")

    try:
        code = asyncio.run(build(out_dir, app_url, site_url, seed))
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if code == 0:
        files = sum(1 for f in out_dir.rglob("*") if f.is_file())
        print(f"\nГотово: {files} файлов в {out_dir}")
        print("Посмотреть локально:  python -m http.server -d dist 8090")
        print("Выложить:             netlify deploy --dir=dist --prod")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
