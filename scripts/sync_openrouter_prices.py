"""Сверка и обновление прайса моделей по каталогу OpenRouter.

Зачем. Себестоимость сервис считает по СВОЕЙ таблице model_prices, а не по
счёту поставщика (так задумано: цена нужна до вызова, чтобы зарезервировать
деньги). Плата за это — прайс может незаметно устареть, и тогда мы торгуем
ниже закупки на каждом вызове. Этот скрипт сверяет нашу таблицу с реальным
каталогом OpenRouter и, по команде, приводит её в порядок.

Задним числом ничего не переписывается: устаревшая строка закрывается
(valid_until = сейчас), рядом заводится новая. История вызовов продолжает
ссылаться на ту цену, которая действовала в момент вызова.

Запуск:
    python scripts/sync_openrouter_prices.py --check      # только показать
    python scripts/sync_openrouter_prices.py --apply      # закрыть и завести
    python scripts/sync_openrouter_prices.py --search claude   # что есть у OpenRouter

В докере:
    docker compose exec neurohub python scripts/sync_openrouter_prices.py --check
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import yaml
from sqlalchemy import or_, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import ModelPrice  # noqa: E402

CATALOG_URL = "https://openrouter.ai/api/v1/models"
MILLION = Decimal(1_000_000)
# Расхождение меньше этого не трогаем: у OpenRouter цены шевелятся в последних
# знаках, и гонять миграцию прайса из-за сотых долей цента бессмысленно.
TOLERANCE_PERCENT = Decimal("1.0")


def _configured_models() -> list[tuple[str, str, str]]:
    """(alias, provider, model) из config/models.yaml."""
    cfg = yaml.safe_load(Path(settings.models_config_path).read_text(encoding="utf-8"))
    out = []
    for entry in cfg["model_list"]:
        full = entry["litellm_params"]["model"]
        provider, _, model = full.partition("/")
        if not model:
            provider, model = "openai", full
        out.append((entry["model_name"], provider, model))
    return out


async def _fetch_catalog() -> dict[str, dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(CATALOG_URL)
        response.raise_for_status()
        return {row["id"]: row for row in response.json()["data"]}


def _per_million(raw: str | None) -> Decimal | None:
    """OpenRouter отдаёт цену за ОДИН токен строкой. Нам нужна за миллион."""
    if raw is None:
        return None
    value = Decimal(str(raw))
    if value <= 0:
        return None
    return (value * MILLION).quantize(Decimal("0.000001"))


def _diff_percent(ours: Decimal | None, theirs: Decimal | None) -> Decimal | None:
    if ours is None or theirs is None or theirs == 0:
        return None
    return ((ours - theirs) / theirs * 100).quantize(Decimal("0.1"))


async def _active_price(session, provider: str, model: str, at: datetime) -> ModelPrice | None:
    rows = (
        await session.execute(
            select(ModelPrice).where(
                ModelPrice.provider == provider,
                ModelPrice.model == model,
                ModelPrice.valid_from <= at,
                or_(ModelPrice.valid_until.is_(None), ModelPrice.valid_until > at),
            )
        )
    ).scalars().all()
    return rows[0] if rows else None


async def run(apply: bool, search: str | None) -> int:
    catalog = await _fetch_catalog()
    print(f"В каталоге OpenRouter: {len(catalog)} моделей\n")

    if search:
        found = sorted(mid for mid in catalog if search.lower() in mid.lower())
        if not found:
            print(f"По запросу «{search}» ничего не нашлось.")
            return 0
        print(f"Найдено по «{search}» ({len(found)}):\n")
        for mid in found[:60]:
            pricing = catalog[mid].get("pricing", {})
            pin = _per_million(pricing.get("prompt"))
            pout = _per_million(pricing.get("completion"))
            price = f"${pin} / ${pout} за 1M" if pin else "цена не указана"
            print(f"  {mid:56} {price}")
        if len(found) > 60:
            print(f"  … и ещё {len(found) - 60}")
        return 0

    now = datetime.now(timezone.utc)
    problems = 0
    planned: list[tuple[ModelPrice | None, str, str, Decimal, Decimal]] = []

    async with SessionLocal() as session:
        for alias, provider, model in _configured_models():
            if provider != "openrouter":
                print(f"— {alias}: возится мимо OpenRouter ({provider}), пропускаю")
                continue

            row = catalog.get(model)
            if row is None:
                print(f"✗ {alias}: модели «{model}» в каталоге OpenRouter НЕТ — вызовы будут падать")
                problems += 1
                continue

            pricing = row.get("pricing", {})
            theirs_in = _per_million(pricing.get("prompt"))
            theirs_out = _per_million(pricing.get("completion"))
            if theirs_in is None or theirs_out is None:
                print(f"✗ {alias}: у OpenRouter не указана цена — завести вручную")
                problems += 1
                continue

            ours = await _active_price(session, provider, model, now)
            if ours is None:
                print(f"+ {alias}: цены у нас нет, будет заведена ${theirs_in} / ${theirs_out} за 1M")
                planned.append((None, provider, model, theirs_in, theirs_out))
                problems += 1
                continue

            d_in = _diff_percent(ours.price_per_1m_input_tokens, theirs_in)
            d_out = _diff_percent(ours.price_per_1m_output_tokens, theirs_out)
            worst = max(abs(d) for d in (d_in, d_out) if d is not None) if (d_in or d_out) else Decimal(0)

            if worst <= TOLERANCE_PERCENT:
                print(f"✓ {alias}: сходится (${theirs_in} / ${theirs_out} за 1M)")
                continue

            sign = "ниже закупки" if (d_in or Decimal(0)) < 0 else "выше закупки"
            print(
                f"! {alias}: наша ${ours.price_per_1m_input_tokens} / "
                f"${ours.price_per_1m_output_tokens}, у OpenRouter ${theirs_in} / ${theirs_out} "
                f"— расхождение {worst}% ({sign})"
            )
            planned.append((ours, provider, model, theirs_in, theirs_out))
            problems += 1

        if not planned:
            print("\nПрайс в порядке, менять нечего.")
            return 0 if problems == 0 else 1

        if not apply:
            print(f"\nК исправлению: {len(planned)}. Запустите с --apply, чтобы применить.")
            return 1

        for old, provider, model, pin, pout in planned:
            if old is not None:
                old.valid_until = now  # закрываем, а НЕ правим — история остаётся верной
            session.add(
                ModelPrice(
                    provider=provider,
                    model=model,
                    price_per_1m_input_tokens=pin,
                    price_per_1m_output_tokens=pout,
                    valid_from=now,
                )
            )
        await session.commit()
        print(f"\nГотово: закрыто строк — {sum(1 for p in planned if p[0] is not None)}, "
              f"заведено новых — {len(planned)}.")
        print("Прошлые вызовы считались по прежним ценам и не пересчитываются.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Сверка прайса моделей с каталогом OpenRouter")
    parser.add_argument("--apply", action="store_true", help="применить изменения, а не только показать")
    parser.add_argument("--check", action="store_true", help="только показать расхождения (по умолчанию)")
    parser.add_argument("--search", metavar="ПОДСТРОКА", help="показать модели OpenRouter по подстроке")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(apply=args.apply, search=args.search)))


if __name__ == "__main__":
    main()
