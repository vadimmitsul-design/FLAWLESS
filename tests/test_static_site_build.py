# -*- coding: utf-8 -*-
"""Сборка витрины в статику (scripts/build_static_site.py).

Проверяются чистые функции, а не прогон целиком: сама сборка поднимает
приложение со своей базой и своим окружением, и запускать её внутри
pytest — значит ломать общую тестовую базу. А вся опасность сборки сидит
именно здесь: ошибка в переписывании ссылок даёт на витрине кнопку «Войти»,
которая ведёт в никуда, и заметить это можно только глазами.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import build_static_site as bss  # noqa: E402

APP = "https://app.flawless.test"
KNOWN = {"/", "/pricing", "/docs", "/docs/billing"}


def _rewrite(html: str):
    moved: dict[str, int] = {}
    return bss._rewrite_links(html, KNOWN, APP, moved), moved


def test_собранные_страницы_остаются_локальными():
    """Иначе каждый переход по витрине уводил бы на домен приложения."""
    out, moved = _rewrite('<a href="/pricing">Цены</a><a href="/">Главная</a>')
    assert out == '<a href="/pricing">Цены</a><a href="/">Главная</a>'
    assert moved == {}


def test_серверные_адреса_уезжают_в_приложение():
    out, moved = _rewrite('<a href="/login">Войти</a><a href="/signup">Начать</a>')
    assert f'href="{APP}/login"' in out
    assert f'href="{APP}/signup"' in out
    assert moved == {"/login": 1, "/signup": 1}


def test_неизвестный_адрес_уезжает_по_умолчанию():
    """Список серверных адресов руками не ведётся специально: новый раздел,
    забытый в списке, иначе дал бы на витрине ссылку в никуда."""
    out, moved = _rewrite('<a href="/совершенно/новый/раздел">?</a>')
    assert f'href="{APP}/совершенно/новый/раздел"' in out
    assert moved == {"/совершенно/новый/раздел": 1}


def test_якорь_и_хвостовой_слэш_не_ломают_опознание():
    out, moved = _rewrite('<a href="/docs/billing#резерв">…</a><a href="/pricing/">…</a>')
    assert APP not in out, "страница опознана как чужая из-за якоря или слэша"
    assert moved == {}


def test_формы_переписываются_тоже():
    """На витрине форм сейчас нет, но появившаяся форма, постящая на статику,
    молча теряла бы данные."""
    out, _ = _rewrite('<form action="/signup" method="post">')
    assert f'action="{APP}/signup"' in out


def test_повторы_считаются_для_отчёта_сборки():
    _out, moved = _rewrite('<a href="/login">1</a><a href="/login">2</a>')
    assert moved == {"/login": 2}


def test_кнопка_в_кабинет_уходит_в_приложение():
    """«/» в шапке документации — это кабинет для вошедшего; на статике «/»
    всегда лендинг, поэтому кнопку нужно увести явно."""
    out = bss._fix_cabinet_link('<a href="/" class="dx-cabinet">В кабинет</a>', APP)
    assert f'<a href="{APP}/" class="dx-cabinet">' in out


def test_логотип_в_шапке_остаётся_на_витрине():
    """Тот же href="/", но у логотипа он означает главную витрины."""
    html = '<a href="/" class="logo">Flawless</a>'
    assert bss._fix_cabinet_link(html, APP) == html
    out, moved = _rewrite(html)
    assert out == html and moved == {}


def test_адреса_раскладываются_каталогами():
    out = Path("/tmp/dist")
    assert bss._out_file(out, "/") == out / "index.html"
    assert bss._out_file(out, "/pricing") == out / "pricing" / "index.html"
    assert bss._out_file(out, "/docs/integrations/cursor") == (
        out / "docs" / "integrations" / "cursor" / "index.html"
    )


def test_метатеги_добавляются_один_раз():
    html = "<head><title>Цены — Flawless</title></head>"
    out = bss._inject_meta(html, "https://flawless.test/pricing", "Описание  страницы")
    assert out.count('rel="canonical"') == 1
    assert '<link rel="canonical" href="https://flawless.test/pricing">' in out
    assert '<meta property="og:title" content="Цены — Flawless">' in out
    assert '<meta name="description" content="Описание страницы">' in out, "переносы не схлопнуты"


def test_у_страницы_404_нет_канонического_адреса():
    """Она отдаётся на ЛЮБОЙ несуществующий адрес: canonical склеил бы весь
    мусор в одну страницу в индексе поисковика."""
    out = bss._inject_meta("<title>Нет такой</title>", None, "Страница не найдена.")
    assert "canonical" not in out
    assert "og:url" not in out
    assert '<meta name="description"' in out


def test_кавычка_в_описании_не_рвёт_атрибут():
    out = bss._inject_meta("<title>X</title>", None, 'цена "под ключ"')
    assert 'content="цена &quot;под ключ&quot;">' in out


def test_редиректы_покрывают_вход_и_регистрацию():
    rules = dict((src, dst) for src, dst, _code in bss._redirect_rules(APP))
    assert rules["/login"] == f"{APP}/login"
    assert rules["/signup"] == f"{APP}/signup"
    assert rules["/admin/*"] == f"{APP}/admin/:splat"


def test_апи_не_редиректится_с_витрины():
    """Редирект POST-запроса к API — источник трудноуловимых ошибок у
    клиентов; API живёт на своём домене."""
    sources = [src for src, _dst, _code in bss._redirect_rules(APP)]
    assert not any(s.startswith("/v1") for s in sources)
