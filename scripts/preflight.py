# -*- coding: utf-8 -*-
"""Проверка файла настроек ПЕРЕД развёртыванием.

Каждая проверка здесь — ошибка, которую этот проект уже совершал и потратил
на неё время. Все они тихие: сервис поднимается, выглядит рабочим, а ломается
позже и не там, где смотрят.

    python scripts/preflight.py .env.internal

Код возврата 1, если есть хоть одна ошибка; предупреждения не валят.
"""

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Значения session_secret, с которыми куки подделываются: они же перечислены
# в app/config.py, но preflight обязан работать без импорта приложения.
WEAK_SECRETS = {
    "change-me",
    "change-me-session-secret",
    "changeme",
    "secret",
    "session-secret",
    "test-secret",
}

errors: list[str] = []
warnings: list[str] = []


def fail(text: str) -> None:
    errors.append(text)


def warn(text: str) -> None:
    warnings.append(text)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def check_self_reference(path: Path, env: dict[str, str]) -> None:
    """ENV_FILE должен называть сам себя.

    Флаг --env-file у docker compose подставляет переменные только в
    compose-файл и внутрь контейнера ничего не передаёт. Без этой строки
    второй экземпляр молча поднимается с настройками первого — на этом
    проект уже обжигался.
    """
    declared = env.get("ENV_FILE", "")
    if not declared:
        fail(f"{path.name}: нет строки ENV_FILE={path.name} — контейнер возьмёт настройки другого экземпляра")
    elif declared != path.name:
        fail(f"{path.name}: ENV_FILE={declared} не совпадает с именем файла")


def check_secrets(path: Path, env: dict[str, str]) -> None:
    secret = env.get("SESSION_SECRET", "").strip()
    if not secret:
        fail(f"{path.name}: SESSION_SECRET пуст — сессионные куки подделываются")
    elif secret.lower() in WEAK_SECRETS or len(secret) < 32:
        fail(f"{path.name}: SESSION_SECRET слабый ({len(secret)} симв.) — нужно не меньше 32 случайных")

    pg = env.get("POSTGRES_PASSWORD", "").strip()
    if not pg:
        warn(f"{path.name}: POSTGRES_PASSWORD не задан — будет умолчание «neurohub»")
    elif pg == "neurohub":
        fail(f"{path.name}: POSTGRES_PASSWORD — умолчание из compose-файла")


def check_environment(path: Path, env: dict[str, str]) -> None:
    """ENVIRONMENT=production включает Secure на сессионной куке.

    По обычному HTTP такая кука не отправляется вовсе: вход молча не
    работает, и в логах об этом ничего нет.
    """
    environment = env.get("ENVIRONMENT", "development").strip()
    if environment not in ("development", "production"):
        fail(f"{path.name}: ENVIRONMENT={environment} — допустимо development или production")
    if environment == "production":
        warn(
            f"{path.name}: ENVIRONMENT=production ставит Secure на сессионную куку. "
            "Без HTTPS перед приложением вход не будет работать и ошибки в логах не появится."
        )
    else:
        warn(
            f"{path.name}: ENVIRONMENT=development — куки без Secure. Это верно для доступа "
            "по HTTP, но пароли при этом идут по сети открытым текстом."
        )


def check_provider_keys(path: Path, env: dict[str, str]) -> None:
    """Каких ключей провайдеров не хватает — по самому реестру моделей.

    Список читается из config/models.yaml, а не перечисляется здесь: иначе
    он отстанет от реестра в тот же день, когда добавят модель.
    """
    config = ROOT / env.get("MODELS_CONFIG_PATH", "config/models.yaml")
    if not config.exists():
        warn(f"не найден реестр моделей {config} — проверку ключей пропускаю")
        return
    needed = set(re.findall(r"api_key:\s*os\.environ/([A-Z0-9_]+)", config.read_text(encoding="utf-8")))
    for name in sorted(needed):
        if not env.get(name, "").strip() and not os.environ.get(name, "").strip():
            fail(f"{path.name}: нет {name} — вызовы моделей упадут на авторизации, сервис будет пустым")


def check_signup(path: Path, env: dict[str, str]) -> None:
    mode = env.get("SIGNUP_MODE", "invite").strip()
    if mode not in ("invite", "open", "closed"):
        fail(f"{path.name}: SIGNUP_MODE={mode} — допустимо invite, open или closed")
    elif mode == "open" and env.get("ENABLE_PUBLIC_SITE", "true").strip().lower() == "false":
        # Открытая регистрация во внутреннем контуре нормальна, если она
        # ограничена доменом рабочей почты. Без ограничения завести аккаунт
        # сможет любой человек из интернета.
        if not env.get("SIGNUP_ALLOWED_EMAIL_DOMAINS", "").strip():
            fail(
                f"{path.name}: SIGNUP_MODE=open без SIGNUP_ALLOWED_EMAIL_DOMAINS — "
                "зарегистрироваться сможет кто угодно с любой почтой"
            )


def check_collisions(path: Path, env: dict[str, str]) -> None:
    """Два экземпляра на одном хосте не должны драться за порты и токен бота.

    Два процесса с одним TELEGRAM_BOT_TOKEN воруют друг у друга сообщения:
    Telegram отдаёт апдейт только одному из них.
    """
    others = [
        other
        for other in sorted(ROOT.glob(".env*"))
        if other.is_file() and not other.name.endswith(".example") and other.resolve() != path.resolve()
    ]
    for other in others:
        try:
            neighbour = read_env(other)
        except OSError:
            continue
        for key in ("APP_PORT", "DB_PORT"):
            mine, theirs = env.get(key, ""), neighbour.get(key, "")
            if mine and mine == theirs:
                fail(f"{path.name} и {other.name} делят {key}={mine} — второй экземпляр не поднимется")
        mine = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if mine and mine == neighbour.get("TELEGRAM_BOT_TOKEN", "").strip():
            fail(
                f"{path.name} и {other.name} используют ОДИН токен Telegram — "
                "два процесса будут воровать друг у друга сообщения"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("env_file", help="файл настроек, например .env.internal")
    args = parser.parse_args()

    path = (ROOT / args.env_file).resolve()
    if not path.exists():
        print(f"ОШИБКА: нет файла {path}")
        return 1

    env = read_env(path)
    for check in (
        check_self_reference,
        check_secrets,
        check_environment,
        check_provider_keys,
        check_signup,
        check_collisions,
    ):
        check(path, env)

    print(f"Проверка {path.name}\n")
    for text in errors:
        print(f"  ОШИБКА      {text}")
    for text in warnings:
        print(f"  внимание    {text}")
    if not errors and not warnings:
        print("  всё чисто")
    print()
    if errors:
        print(f"Ошибок: {len(errors)}. Разворачивать в таком виде нельзя.")
        return 1
    print("Блокирующих ошибок нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
