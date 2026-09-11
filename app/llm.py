"""Обёртка над LiteLLM Router. Модели описаны в config/models.yaml —
добавление модели не должно трогать этот модуль. В отличие от AI-HUB/gateway
клиент сам указывает model_name (alias) в запросе — это и есть публичный
контракт /v1/chat/completions.
"""

from pathlib import Path
from typing import Any

import litellm
import yaml
from litellm import Router

from app.config import settings
from app.pricing import UsageAmounts

litellm.drop_params = True  # молча отбрасывать параметры, которые модель не знает

_router: Router | None = None
_alias_map: dict[str, tuple[str, str]] = {}  # alias -> (provider, model)
_fallback_map: dict[str, list[str]] = {}  # alias -> [alias, ...] в порядке приоритета

# Ошибки уровня провайдера, при которых имеет смысл попробовать другую модель
# вместо немедленного отказа клиенту. NotFoundError/AuthenticationError и т.п.
# намеренно не входят — это конфигурационные ошибки, ретраить бессмысленно.
FALLBACK_TRIGGER_ERRORS = (
    litellm.RateLimitError,
    litellm.Timeout,
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
)


def _load_config() -> dict:
    path = Path(settings.models_config_path)
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def init_router() -> None:
    global _router, _alias_map, _fallback_map
    cfg = _load_config()
    model_list = cfg["model_list"]
    _alias_map = {}
    _fallback_map = {}
    for entry in model_list:
        full = entry["litellm_params"]["model"]
        provider, _, model = full.partition("/")
        if not model:
            provider, model = "openai", full
        _alias_map[entry["model_name"]] = (provider, model)
        _fallback_map[entry["model_name"]] = entry.get("model_info", {}).get("fallback", [])
    _router = Router(
        model_list=model_list,
        num_retries=settings.llm_num_retries,
        timeout=settings.llm_timeout_seconds,
    )


def known_models() -> list[str]:
    return list(_alias_map.keys())


def alias_for(provider: str, model: str) -> str | None:
    """(provider, model) -> алиас, если такая модель есть в реестре.

    Нужна интерфейсу: в usage_events хранится пара провайдера, а клиент
    знает модель по короткому алиасу — тому, что он пишет в запросе.
    Показывать ему «anthropic/claude-sonnet-5» вместо «claude-sonnet»
    значит показывать чужое имя.
    """
    for alias, pair in _alias_map.items():
        if pair == (provider, model):
            return alias
    return None


def resolve_alias(alias: str) -> tuple[str, str]:
    """alias из тела запроса ("model") -> (provider, model) для прайса и учёта."""
    if alias not in _alias_map:
        raise KeyError(f"model '{alias}' is not defined in {settings.models_config_path}")
    return _alias_map[alias]


async def chat_completion(alias: str, messages: list[dict], **kwargs: Any):
    assert _router is not None, "init_router() was not called"
    return await _router.acompletion(model=alias, messages=messages, **kwargs)


async def chat_completion_with_fallback(
    alias: str, messages: list[dict], **kwargs: Any
) -> tuple[str, str, str, Any]:
    """Пробует alias, при ошибках уровня провайдера (rate limit/timeout/5xx) —
    по очереди алиасы из fallback-цепочки config/models.yaml. Возвращает
    (alias_использован, provider, model, response) — именно по этой тройке
    считается себестоимость и биллится клиент, а не по изначально запрошенной
    модели. Явный питоновский цикл, а не встроенный router.fallbacks —
    чтобы точно знать, какая модель реально ответила, для корректного учёта.
    """
    chain = [alias] + _fallback_map.get(alias, [])
    last_exc: Exception | None = None
    for candidate in chain:
        try:
            provider, model = resolve_alias(candidate)
            response = await chat_completion(candidate, messages, **kwargs)
            return candidate, provider, model, response
        except FALLBACK_TRIGGER_ERRORS as e:
            last_exc = e
            continue
    assert last_exc is not None
    raise last_exc


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def extract_chat_usage(response: Any) -> UsageAmounts:
    usage = _field(response, "usage")
    details = _field(usage, "prompt_tokens_details")
    return UsageAmounts(
        input_text_tokens=_field(usage, "prompt_tokens"),
        cached_tokens=_field(details, "cached_tokens"),
        # Anthropic (через LiteLLM) кладёт запись в кэш отдельным полем
        # cache_creation_input_tokens прямо на usage, не в prompt_tokens_details —
        # не проверено боевым вызовом на Claude, см. CLAUDE.md.
        cache_write_tokens=_field(usage, "cache_creation_input_tokens"),
        output_tokens=_field(usage, "completion_tokens"),
    )


def extract_litellm_cost(response: Any) -> float | None:
    hidden = getattr(response, "_hidden_params", None) or {}
    return _field(hidden, "response_cost")


def extract_call_id(response: Any) -> str | None:
    hidden = getattr(response, "_hidden_params", None) or {}
    return _field(hidden, "litellm_call_id") or _field(response, "id")


def to_dict(response: Any) -> dict:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if hasattr(response, "dict"):
        return response.dict()
    return dict(response)
