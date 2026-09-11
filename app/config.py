from pydantic_settings import BaseSettings, SettingsConfigDict

# Значения session_secret, которые нельзя пускать в прод: дефолт из кода,
# плейсхолдер из .env.example и типичные заглушки. Раньше проверка в main.py
# сравнивала только со строкой "change-me", а в .env.example лежит
# "change-me-session-secret" — другая строка, и защита не срабатывала никогда.
_INSECURE_SESSION_SECRETS = {
    "change-me",
    "change-me-session-secret",
    "changeme",
    "secret",
    "session-secret",
    "test-secret",
}
_MIN_SESSION_SECRET_LEN = 32


def session_secret_is_weak(secret: str) -> bool:
    """Кука сессии подписывается этим значением (не шифруется). Угадал строку —
    подписал себе куку любого клиента, включая админа."""
    normalized = secret.strip()
    return normalized.lower() in _INSECURE_SESSION_SECRETS or len(normalized) < _MIN_SESSION_SECRET_LEN


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://neurohub:neurohub@localhost:5434/neurohub"
    session_secret: str = "change-me"
    # Кто может завести аккаунт:
    #   invite — только по одноразовому коду от админа (дефолт: закрытый контур)
    #   open   — любой желающий (публичный реселлинг, второй этап)
    #   closed — регистрация выключена совсем, аккаунты заводит админ
    signup_mode: str = "invite"
    # Подпись экземпляра в шапке кабинета ("внутренний" / "клиентский").
    # Один и тот же код поднимается двумя экземплярами со своими базами;
    # без подписи две одинаковые админки легко перепутать.
    instance_name: str = ""

    # Адрес, который показывается клиенту в документации и на лендинге
    # (base_url для OpenAI SDK). Домена пока нет — когда появится, меняется
    # ОДНОЙ переменной, а не правкой каждой страницы документации.
    public_base_url: str = "https://api.flawless.ru"
    # Необязательные разделы продукта. Внутреннему кабинету для своих
    # разработчиков они не нужны, а в магазине, платных промптах и детских
    # аккаунтах живёт бОльшая часть находок аудита 2026-09-07 — выключенный
    # раздел не только не мозолит глаза, но и закрывает свои маршруты
    # (404), иначе адрес продолжал бы работать в обход интерфейса.
    enable_shop: bool = True
    enable_prompts: bool = True
    enable_children: bool = True
    enable_archive: bool = True
    # "production" включает Secure-флаг на сессионной куке (main.py) и
    # запрещает дефолтный session_secret при старте. Локально/в docker compose
    # без TLS оставлять "development" — иначе кука не будет отправляться по HTTP.
    environment: str = "development"
    models_config_path: str = "config/models.yaml"
    llm_num_retries: int = 2
    llm_timeout_seconds: int = 120
    rate_limit_per_window: int = 30
    rate_limit_window_seconds: int = 60
    telegram_bot_token: str = ""
    telegram_default_model: str = "gpt-5-mini"
    # Резерв под вызов (1.1 доработок) считается по max_tokens клиента, если
    # он есть, иначе по этому дефолту — сознательно с запасом.
    default_max_output_tokens_estimate: int = 4096
    # Жёсткий потолок длины ответа, который ВСЕГДА уходит провайдеру
    # (см. pricing.clamp_output_tokens). Без него один ответ мог кратно
    # пробить резерв и увести баланс в минус — списание по факту делается
    # без проверки баланса. Клиентское значение зажимается сверху.
    max_output_tokens_cap: int = 4096
    # Запас поверх грубой оценки резерва (1.1) — эвристика ~4 символа/токен
    # занижает не-латинские языки (кириллица ближе к ~2-2.5 симв/токен, а это
    # основной язык клиентов), и не учитывает премию за запись в кэш. Найдено
    # состязательным ревью 2026-09-04, см. CLAUDE.md.
    reserve_safety_margin: float = 1.5
    # Резерв, когда прайса вообще нет (новая модель без сида, просроченная
    # строка цены) — раньше падало на 0₽, что полностью отключало защиту
    # резервом именно в этом случае. Найдено состязательным ревью 2026-09-04.
    fallback_reserve_rub_when_unpriced: float = 50.0
    # Уборщик зависших pending-событий (app/reaper.py) — если процесс упал
    # целиком между start_call и finalize_*, ни except, ни finally не
    # выполнятся вообще, резерв виснет навсегда без внешней подметки.
    # С запасом над llm_num_retries * llm_timeout_seconds (сейчас 2*120=240с).
    stale_pending_reap_after_seconds: int = 600
    reaper_interval_seconds: int = 120
    # Контроль маржи по моделям (1.6 доработок) — ниже этого % (или отрицательная)
    # маржа подсвечивается в /admin/overview и /admin/reconciliation как сигнал,
    # а не тонет тихо в среднем по всем моделям.
    margin_alert_threshold_pct: float = 15.0
    # Сверка с поставщиком (1.5 доработок) — расхождение между нашей себестоимостью
    # (model_prices) и контрольным litellm_cost выше этого % подсвечивается за день.
    cost_discrepancy_alert_threshold_pct: float = 10.0


settings = Settings()
