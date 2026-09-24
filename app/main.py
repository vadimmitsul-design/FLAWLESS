"""Application composition and ASGI entry point."""

from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.api.admin.auth import router as api_admin_auth_router
from app.api.admin.customers import router as api_admin_customers_router
from app.api.admin.invites import router as api_admin_invites_router
from app.api.admin.keys import router as api_admin_keys_router
from app.api.admin.pricing import router as api_admin_pricing_router
from app.api.admin.reporting import router as api_admin_reporting_router
from app.api.admin.resources import router as api_admin_resources_router
from app.api.admin.shop import router as api_admin_shop_router
from app.api.admin.topups import router as api_admin_topups_router
from app.api.archive import router as api_archive_router
from app.api.auth import router as api_auth_router
from app.api.cabinet import router as api_cabinet_router
from app.api.cabinet_json import router as api_cabinet_json_router
from app.api.chat import router as api_chat_router
from app.api.children import router as api_children_router
from app.api.errors import domain_error_response
from app.api.health import router as api_health_router
from app.api.prompts import router as api_prompts_router
from app.api.public import router as api_public_router
from app.api.resources import router as api_resources_router
from app.api.shop import router as api_shop_router
from app.api.telegram import router as api_telegram_router
from app.api.v1.chat import router as api_v1_chat_router
from app.api.v1.models import router as api_v1_models_router
from app.core.config import settings
from app.core.csrf import CSRFOriginMiddleware
from app.core.errors import DomainError
from app.core.lifecycle import lifespan
from app.core.middleware import BodySizeLimitMiddleware, NoStoreMiddleware
from app.core.templating import create_templates


def create_app() -> FastAPI:
    """Build the application with its middleware and feature routers."""
    app = FastAPI(
        title="Flawless",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.templates = create_templates(settings)
    app.add_exception_handler(DomainError, domain_error_response)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(CSRFOriginMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.environment == "production",
        same_site="lax",
    )
    app.add_middleware(NoStoreMiddleware)
    app.include_router(api_health_router)
    app.include_router(api_auth_router)
    app.include_router(api_public_router)
    app.include_router(api_resources_router)
    app.include_router(api_admin_resources_router)
    app.include_router(api_cabinet_router)
    app.include_router(api_cabinet_json_router)
    app.include_router(api_shop_router)
    app.include_router(api_prompts_router)
    app.include_router(api_archive_router)
    app.include_router(api_children_router)
    app.include_router(api_telegram_router)
    app.include_router(api_admin_reporting_router)
    app.include_router(api_admin_topups_router)
    app.include_router(api_admin_auth_router)
    app.include_router(api_admin_shop_router)
    app.include_router(api_admin_customers_router)
    app.include_router(api_admin_pricing_router)
    app.include_router(api_admin_invites_router)
    app.include_router(api_admin_keys_router)
    app.include_router(api_v1_chat_router)
    app.include_router(api_v1_models_router)
    app.include_router(api_chat_router)

    return app


app = create_app()
