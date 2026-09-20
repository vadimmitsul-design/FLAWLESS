# -*- coding: utf-8 -*-
"""Сменить пароль администратора на случайный, не показывая его в консоли.

Зачем отдельный скрипт, если есть manage_admin.py: там пароль передаётся
аргументом `--password` и оседает в истории оболочки открытым текстом
(PowerShell: PSReadLine\\ConsoleHost_history.txt). Здесь пароль
генерируется внутри, уходит ТОЛЬКО в файл `--out`, а в stdout — только
статус. Ни в истории команд, ни в логах, ни в переписке его нет.

Проверка настоящая: после записи хеша скрипт делает POST /login на
работающий сервер и убеждается, что тот пустил, — а не просто сверяет
хеш сам с собой.

Запуск внутри контейнера (проект в образ запечён, тома нет — поэтому
скрипт заносится через docker cp, а файл с паролем забирается им же):

  docker cp scripts/rotate_admin_password.py <контейнер>:/tmp/rotate.py
  docker exec <контейнер> python /tmp/rotate.py \\
      --email vadim@example.com --label internal \\
      --url http://localhost:8040 --out /tmp/creds.txt
  docker cp <контейнер>:/tmp/creds.txt secrets/admin-credentials.txt
  docker exec <контейнер> rm -f /tmp/rotate.py /tmp/creds.txt

Из Git Bash на Windows НЕ запускать: MSYS переписывает `/tmp/...` в
`C:/Users/.../Temp/...`, и python не находит файл. PowerShell — можно.
"""

import argparse
import asyncio
import http.client
import secrets
import string
import sys
import urllib.parse
from pathlib import Path

# Скрипт по документированному выше сценарию лежит в /tmp/rotate.py — родитель
# родителя такого пути это "/", и там нет каталога app. Вычисляем путь ДВУМЯ
# способами и берём первый, где app/ реально нашёлся: "рядом с самим файлом"
# работает при запуске из настоящего checkout (scripts/rotate_admin_password.py),
# /neurohub — фиксированный WORKDIR образа (см. Dockerfile) и есть всегда,
# когда скрипт запущен внутри контейнера через docker exec, независимо от
# того, куда его занесли. Без этой развилки первый прогон падал уже на
# импорте: ModuleNotFoundError: No module named 'app'.
for _root in (Path(__file__).resolve().parent.parent, Path("/neurohub")):
    if (_root / "app").is_dir():
        sys.path.insert(0, str(_root))
        break
else:
    raise SystemExit(f"не нашёл каталог app/ ни рядом со скриптом, ни в /neurohub")

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Customer  # noqa: E402
from app.security import hash_password, verify_password  # noqa: E402

# Без похожих символов (0/O, 1/l/I) — пароль будут набирать глазами
# с листа, а не вставлять. Знаки — только те, что не спорят с оболочкой.
ALPHABET = (
    "".join(c for c in string.ascii_letters if c not in "lIO")
    + "".join(c for c in string.digits if c not in "01")
    + "-_.!"
)
LENGTH = 20

# Ключ передаётся аргументом (ASCII — кириллица в аргументах через Git Bash
# на Windows ненадёжна), заголовок для файла подставляется здесь.
LABELS = {
    "internal": "Внутренний контур (для сотрудников)",
    "market": "Контур для рынка (внешние клиенты)",
}


def generate() -> str:
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
        # Гарантия всех классов — иначе редкий, но возможный пароль
        # из одних букв выглядел бы слабее, чем есть.
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in "-_.!" for c in pw)
        ):
            return pw


async def rotate(email: str, password: str) -> str:
    async with SessionLocal() as session:
        customer = (
            await session.execute(select(Customer).where(Customer.email == email))
        ).scalar_one_or_none()
        if customer is None:
            raise SystemExit(f"аккаунт {email} не найден — ничего не менял")
        customer.password_hash = hash_password(password)
        customer.role = "admin"
        customer.active = True
        await session.commit()
        return customer.password_hash


def login_works(email: str, password: str) -> tuple[bool, str]:
    """Настоящий вход через HTTP. Успех — редирект 303 с cookie сессии."""
    body = urllib.parse.urlencode({"email": email, "password": password})
    conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=15)
    conn.request(
        "POST",
        "/login",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = conn.getresponse()
    resp.read()
    cookie = resp.getheader("set-cookie") or ""
    ok = resp.status == 303 and bool(cookie)
    return ok, f"HTTP {resp.status}, cookie={'есть' if cookie else 'нет'}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--label", required=True, choices=sorted(LABELS))
    parser.add_argument("--url", required=True, help="адрес входа — для файла")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    email = args.email.strip().lower()
    password = generate()

    stored_hash = asyncio.run(rotate(email, password))

    # Файл — СРАЗУ после коммита в базу, до любых проверок. Иначе провал
    # проверки оставил бы новый хеш в базе и пароль — нигде: блокировка.
    title = LABELS[args.label]
    Path(args.out).write_text(
        f"{title}\n"
        f"  адрес:  {args.url}\n"
        f"  логин:  {email}\n"
        f"  пароль: {password}\n"
        f"\n",
        encoding="utf-8",
    )

    if not verify_password(password, stored_hash):
        raise SystemExit("хеш не сходится с паролем — файл записан, но это ошибка")

    ok, detail = login_works(email, password)
    if not ok:
        raise SystemExit(f"пароль записан в файл, но сервер НЕ пустил: {detail}")

    print(f"{title}: пароль сменён, вход проверен ({detail})")


if __name__ == "__main__":
    main()
