"""Create a template environment with only public configuration values."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.config import Settings
from app.core.paths import BASE_DIR


@dataclass(frozen=True, slots=True)
class FeatureFlags:
    """The allowlisted settings visible to templates; never includes credentials."""

    instance_name: str
    api_base_url: str
    enable_public_site: bool
    enable_resources: bool
    enable_shop: bool
    enable_prompts: bool
    enable_children: bool
    enable_archive: bool

    @classmethod
    def from_settings(cls, config: Settings) -> "FeatureFlags":
        return cls(
            instance_name=config.instance_name,
            api_base_url=config.public_base_url,
            enable_public_site=config.enable_public_site,
            enable_resources=config.enable_resources,
            enable_shop=config.enable_shop,
            enable_prompts=config.enable_prompts,
            enable_children=config.enable_children,
            enable_archive=config.enable_archive,
        )


def money(value: Decimal | float | int | str | None, decimals: int = 2) -> str:
    """Format rubles without converting Decimal balances to binary floats."""
    if value is None:
        return "—"
    return f"{Decimal(str(value)):,.{decimals}f}".replace(",", "\u00a0").replace(".", ",")


def thousands(value: int | str | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}".replace(",", "\u00a0")


def create_templates(config: Settings) -> Jinja2Templates:
    """Own template state per app and refresh feature flags for each request."""

    def public_context(request: Request) -> dict[str, Any]:
        return {"features": FeatureFlags.from_settings(request.app.state.settings)}

    templates = Jinja2Templates(
        directory=str(BASE_DIR / "templates"), context_processors=[public_context]
    )
    templates.env.filters["money"] = money
    templates.env.filters["thousands"] = thousands
    templates.env.globals["features"] = FeatureFlags.from_settings(config)
    return templates
