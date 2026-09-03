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


settings = Settings()
