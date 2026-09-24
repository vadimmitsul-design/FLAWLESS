"""services / catalog for the Flawless application."""

import logging
import os
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    settings,
)
from app.db import SessionLocal
from app.db.models import (
    ModelPrice,
    PricingConfig,
    utcnow,
)
from app.integrations import llm
from app.services import billing, pricing

logger = logging.getLogger(__name__)


# ---------- публичные страницы: лендинг и документация ----------

_VENDOR_TITLES = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "gemini": "Google",
    "google": "Google",
    "meta-llama": "Meta",
    "mistralai": "Mistral",
    "deepseek": "DeepSeek",
    "qwen": "Qwen",
    "x-ai": "xAI",
    "openrouter": "OpenRouter",
}


def _vendor_title(provider: str, model: str) -> str:
    """Кто сделал модель — для витрины.

    При закупке через OpenRouter провайдер у всех моделей один, а
    производитель зашит в идентификатор: anthropic/claude-sonnet-4-6.
    Показывать клиенту «OpenRouter» вместо «Anthropic» бессмысленно —
    он выбирает модель, а не поставщика трафика.
    """
    vendor = model.split("/", 1)[0] if provider == "openrouter" and "/" in model else provider
    return _VENDOR_TITLES.get(vendor, vendor)


_MODEL_BLURBS = {
    "gpt-5-mini": "Быстрые и недорогие ответы для основной массы запросов.",
    "claude-sonnet": "Длинный контекст и задачи, где важна точность рассуждения.",
    "gemini-flash": "Низкая задержка, сильна в работе с большими документами.",
    "gemini-flash-lite": "Самая дешёвая в каталоге — для массовых и черновых задач.",
}


def _fmt_rub(value: Decimal) -> str:
    """Разряды — неразрывным пробелом, чтобы цена не переносилась по строкам."""
    return f"{value:,.0f}".replace(",", "\u00a0")


async def report_model_readiness() -> None:
    """Сказать при старте, какие модели реально можно вызвать.

    Зачем. Сервис узнаёт о непригодной модели только в момент платного
    вызова: нет цены — 503, нет ключа провайдера — ошибка авторизации.
    Так уже случилось на практике: при переезде закупки на OpenRouter в базе
    остались строки прайса под старые пары (openai/gpt-5-mini), новые пары
    (openrouter + openai/gpt-5-mini) цены не нашли, и список моделей стал
    пустым — в чате открывался пустой выпадающий список. Молчать про это
    до первого вызова нельзя.
    """
    missing_keys: set[str] = set()
    try:
        cfg = yaml.safe_load(Path(settings.models_config_path).read_text(encoding="utf-8"))
        for entry in cfg.get("model_list", []):
            raw = entry.get("litellm_params", {}).get("api_key", "")
            if isinstance(raw, str) and raw.startswith("os.environ/"):
                name = raw.split("/", 1)[1]
                if not os.environ.get(name):
                    missing_keys.add(name)
    except Exception:  # конфиг уже прочитан init_router — здесь только диагностика
        logger.warning("не удалось разобрать %s для проверки ключей", settings.models_config_path)

    if missing_keys:
        logger.error(
            "нет переменных окружения с ключами: %s — вызовы этих моделей упадут на авторизации",
            ", ".join(sorted(missing_keys)),
        )

    try:
        async with SessionLocal() as session:
            rows = await _model_rows(session)
    except Exception as exc:
        logger.warning("проверка прайса при старте не выполнена: %s", exc)
        return

    unpriced = [
        alias
        for alias, _p, _m, price in rows
        if price is None or price.price_per_1m_input_tokens is None
    ]
    usable = len(rows) - len(unpriced)
    if unpriced:
        logger.error(
            "без действующей цены и потому недоступны: %s — заведите прайс "
            "(scripts/seed_prices.py) или уберите модель из %s",
            ", ".join(unpriced),
            settings.models_config_path,
        )
    if usable == 0 and rows:
        logger.error(
            "НИ ОДНА модель не доступна для вызова: список моделей пуст во всём "
            "интерфейсе, а любой вызов вернёт 503"
        )
    else:
        logger.info("моделей готово к вызову: %d из %d", usable, len(rows))


async def _model_rows(session: AsyncSession) -> list[tuple[str, str, str, ModelPrice | None]]:
    """(алиас, провайдер, модель, действующая строка прайса) по всем моделям
    из config/models.yaml. Единственное место, связывающее реестр моделей с
    прайсом, — чтобы «что показываем» и «что вызывается» не разъезжались."""
    now = utcnow()
    rows: list[tuple[str, str, str, ModelPrice | None]] = []
    for alias in llm.known_models():
        provider, model = llm.resolve_alias(alias)
        price = await pricing.find_price(session, provider, model, None, None, now)
        rows.append((alias, provider, model, price))
    return rows


async def available_models(session: AsyncSession) -> list[str]:
    """Модели, которые реально можно вызвать, — то есть с действующей ценой.

    Без цены вызов отклоняется с 503 (billing.price_for_call), поэтому
    предлагать такую модель в выборе — значит обещать заведомую ошибку.
    Используется ВЕЗДЕ, где показывается список: кабинет, веб-чат,
    /v1/models. Отфильтровать один список мало — в остальных модель
    по-прежнему предлагалась бы и падала при отправке.
    """
    return [
        alias
        for alias, _provider, _model, price in await _model_rows(session)
        if price is not None and price.price_per_1m_input_tokens is not None
    ]


async def public_model_catalog(session: AsyncSession) -> tuple[list[dict], PricingConfig]:
    """Каталог моделей с ценой в рублях для лендинга и документации.

    Цена считается из той же таблицы и по той же формуле, что и реальное
    списание (billing.price_in_rub) — иначе витрина и счёт разъехались бы,
    а клиент узнавал бы настоящую цену только из истории вызовов.
    """
    cfg = await billing.get_pricing_config(session)
    catalog: list[dict] = []
    for alias, provider, model, price in await _model_rows(session):
        input_rub: Decimal | None = None
        output_rub: Decimal | None = None
        if price is not None and price.price_per_1m_input_tokens is not None:
            input_rub = billing.price_in_rub(price.price_per_1m_input_tokens, cfg)
            output_rub = billing.price_in_rub(price.price_per_1m_output_tokens or Decimal(0), cfg)
        catalog.append(
            {
                "alias": alias,
                "model": model,
                "vendor": _vendor_title(provider, model),
                "blurb": _MODEL_BLURBS.get(alias, "Доступна через тот же ключ и тот же баланс."),
                "priced": input_rub is not None,
                "price_in": _fmt_rub(input_rub) if input_rub is not None else "",
                "price_out": _fmt_rub(output_rub) if output_rub is not None else "",
                # числом — для калькулятора на лендинге
                "rub_in": float(input_rub) if input_rub is not None else 0.0,
                "rub_out": float(output_rub) if output_rub is not None else 0.0,
            }
        )
    return catalog, cfg


async def public_page_context(session: AsyncSession) -> dict:
    """Общий контекст лендинга и документации: живые цены вместо заглушек.

    Наценка и курс раньше стояли на лендинге литералом «[НАЦЕНКА]%» —
    страница врала бы клиенту в тот же день, когда админ поменяет прайс.
    """
    models, cfg = await public_model_catalog(session)
    priced = [m for m in models if m["priced"]]
    # Числа для калькулятора и примера отчёта считаются из тех же цен, что и
    # витрина: иначе «посчитайте сами» показывало бы одно, а счёт — другое.
    calc_rows = [
        {"alias": m["alias"], "vendor": m["vendor"], "rub_in": m["rub_in"], "rub_out": m["rub_out"]}
        for m in priced
    ]
    sample_ledger = []
    for m, (tin, tout) in zip(priced, ((1840, 620), (5210, 1480), (960, 310)), strict=False):
        rub = m["rub_in"] * tin / 1_000_000 + m["rub_out"] * tout / 1_000_000
        sample_ledger.append(
            {
                "model": m["alias"],
                "tokens": f"{tin + tout:,}".replace(",", " ") + " токенов",
                "rub": f"{rub:,.2f}".replace(",", " ").replace(".", ","),
            }
        )
    return {
        "customer": None,
        # Витрина всегда тёмная (.surface-dark), поэтому и документация,
        # как её часть, открывается тёмной — иначе переход с лендинга в
        # справочник читается как самопроизвольная смена темы. Во внутреннем
        # контуре витрины нет, документация там часть кабинета и слушается
        # общей темы. Явный выбор пользователя перевешивает это умолчание.
        "dark_default": settings.enable_public_site,
        "models": models,
        "calc_rows": calc_rows,
        "sample_ledger": sample_ledger,
        # normalize() убирает хвостовые нули (30.00 -> 30), а ":f" не даёт ему
        # свалиться в экспоненту: Decimal("30.00").normalize() это 3E+1.
        "markup_percent": f"{cfg.markup_percent.normalize():f}",
        "usd_rub_rate": f"{cfg.usd_rub_rate.normalize():f}",
        # Первая ПРОЦЕНЁННАЯ, а не просто первая: примеры curl и Python на
        # лендинге и во всей документации подставляют этот алиас, а вызов
        # модели без действующей цены отклоняется с 503. Документация звала
        # бы читателя на заведомо ломающийся запрос.
        "default_model": next(
            (m["alias"] for m in models if m["priced"]),
            models[0]["alias"] if models else "gpt-5-mini",
        ),
        "max_output_tokens_cap": settings.max_output_tokens_cap,
        "rate_limit_per_window": settings.rate_limit_per_window,
        "rate_limit_window_seconds": settings.rate_limit_window_seconds,
    }


def _vendor_owner(provider: str, model: str) -> str:
    """owned_by в формате OpenAI: машинное имя производителя, не витринное."""
    return model.split("/", 1)[0] if provider == "openrouter" and "/" in model else provider


def _model_object(alias: str, provider: str, price: ModelPrice) -> dict:
    """Формат OpenAI. created берём из даты начала действия прайса — это
    единственная осмысленная дата, которая у нас есть, и она стабильна
    (часть клиентов сортирует список по ней)."""
    return {
        "id": alias,
        "object": "model",
        "created": int(price.valid_from.timestamp()),
        "owned_by": provider,
    }


async def catalog_objects(session: AsyncSession) -> list[dict]:
    return [
        _model_object(alias, _vendor_owner(provider, model), price)
        for alias, provider, model, price in await _model_rows(session)
        if price is not None and price.price_per_1m_input_tokens is not None
    ]
