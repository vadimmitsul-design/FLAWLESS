# Проверки

Поддерживаемое окружение: Python 3.12 и PostgreSQL 16. Установка:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.lock
```

Быстрые регрессии используют SQLite и подменённые ответы моделей:

```powershell
.venv/Scripts/python -m pytest tests -q
```

Этот набор проверяет HTTP-контракты и бизнес-сценарии. Он не проверяет
PostgreSQL-блокировки и цепочку Alembic. Тесты сохраняют историческую общую
SQLite-базу на прогон; одновременно запускать два процесса с `tests/` нельзя.

Для проверки транзакций есть отдельный набор `integration_tests/`. Он не
запускает ASGI-приложение и не обращается к LLM или Telegram. Каждый тест получает
очищенную PostgreSQL-базу, подготовленную через `alembic upgrade head`, и
отдельные соединения для конкурентных операций. Проверяются резервирование,
идемпотентность, покупка одновременно с резервом, финализация, встречные
роялти, освобождение просроченных резервов, регистрация с одинаковой почтой и
одновременное использование одного приглашения.

```powershell
docker compose -p flawless-tests -f compose.test.yml up -d --wait
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://flawless_test:flawless_test@127.0.0.1:55432/flawless_test'
.venv/Scripts/python -m pytest integration_tests -q
$env:DATABASE_URL = $env:TEST_DATABASE_URL
.venv/Scripts/python -m alembic check
docker compose -p flawless-tests -f compose.test.yml down
```

Контейнер хранит данные в `tmpfs`, использует отдельный порт и не подключает
тома приложения. Тесты разрешают только локальную БД с именем `flawless_test`;
перед каждым тестом её таблицы очищаются. Запускайте PostgreSQL-набор отдельным
процессом от `tests/` и без pytest-xdist. Без `TEST_DATABASE_URL` этот набор
явно пропускается; в CI переменная задана и тесты обязательны.

В Bash вместо строки PowerShell задайте:

```bash
export TEST_DATABASE_URL='postgresql+asyncpg://flawless_test:flawless_test@127.0.0.1:55432/flawless_test'
```

Стиль, базовая статическая проверка и типизация:

```powershell
.venv/Scripts/python -m ruff check app scripts tests integration_tests
.venv/Scripts/python -m ruff format --check app scripts tests integration_tests
.venv/Scripts/python -m mypy
```

Ruff проверяет все Python-файлы приложения, скриптов и тестов. Mypy проверяет
весь `app/` и PostgreSQL-тесты, включая тела функций без аннотаций. В `core`,
`services`, `db` и PostgreSQL-тестах аннотации функций обязательны. Область и
правила явно перечислены в `pyproject.toml`; исторические HTTP-обработчики ещё
не переведены целиком в строгий режим. Импортируемые внешние модули дают типы
без дополнительной проверки их реализации (`follow_imports = "silent"`).

GitHub Actions повторяет эти команды на Python 3.12 с отдельным PostgreSQL 16.
Реальные ключи провайдеров и рабочая база для проверок не требуются.

`requirements.txt` содержит прямые зависимости приложения; Docker устанавливает
их из `requirements.lock`. Разработка использует те же версии через constraint
в `requirements-dev.txt`, а `requirements-dev.lock` фиксирует полное окружение
с маркерами Windows/Linux. Обновляйте оба lock-файла осознанно, затем повторяйте
обе группы тестов:

```powershell
uv pip compile requirements.txt --universal --python-version 3.12 --output-file requirements.lock --no-annotate --no-header
uv pip compile requirements-dev.txt --universal --python-version 3.12 --output-file requirements-dev.lock --no-annotate --no-header
```
