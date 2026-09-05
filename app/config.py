from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://neurohub:neurohub@localhost:5434/neurohub"
    session_secret: str = "change-me"
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
