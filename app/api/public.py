"""api / public for the Flawless application."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import _feature_public_site, get_current_customer
from app.core.pages import DOCS_INDEX, DOCS_NAV, DOCS_PAGES, DOCS_SEARCH_INDEX, MARKETING_INDEX
from app.db import get_session
from app.db.models import (
    Customer,
)
from app.services.catalog import public_page_context

router = APIRouter()


async def _render_doc(request: Request, path: str, session: AsyncSession, signed_in: bool = False):
    group, _path, title, template, lead = DOCS_PAGES[DOCS_INDEX[path]]
    index = DOCS_INDEX[path]
    ctx = await public_page_context(session)
    ctx.update(
        {
            # Шапка одна на всю витрину, поэтому вошедшему она показывает
            # «В кабинет», а гостю — «Войти». Сам customer в контекст не
            # кладётся: по нему base.html рисует ШАПКУ КАБИНЕТА, и на
            # странице оказалось бы две шапки подряд.
            "signed_in": signed_in,
            "here": "docs",
            "docs_nav": DOCS_NAV,
            "docs_search": DOCS_SEARCH_INDEX,
            "active_path": path,
            "page_title": title,
            "page_group": group,
            "page_lead": lead,
            "prev_page": (
                {"path": DOCS_PAGES[index - 1][1], "title": DOCS_PAGES[index - 1][2]}
                if index > 0
                else None
            ),
            "next_page": (
                {"path": DOCS_PAGES[index + 1][1], "title": DOCS_PAGES[index + 1][2]}
                if index + 1 < len(DOCS_PAGES)
                else None
            ),
        }
    )
    return request.app.state.templates.TemplateResponse(request, template, ctx)


@router.get("/docs")
async def docs_index(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    return await _render_doc(request, "/docs", session, signed_in=customer is not None)


# slug:path, а не slug: адреса интеграций вложенные (/docs/integrations/cursor).
@router.get("/docs/{slug:path}")
async def docs_page(
    slug: str,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    path = f"/docs/{slug}"
    if path not in DOCS_INDEX:
        raise HTTPException(status_code=404)
    return await _render_doc(request, path, session, signed_in=customer is not None)


async def _render_marketing(
    request: Request, path: str, session: AsyncSession, signed_in: bool = False
):
    _path, template, title, kicker, h1, lead, here = MARKETING_INDEX[path]
    ctx = await public_page_context(session)
    ctx.update(
        {
            "signed_in": signed_in,
            "page_title": title,
            "page_kicker": kicker,
            "page_h1": h1,
            "page_lead": lead,
            "page_cta": True,
            "here": here,
        }
    )
    return request.app.state.templates.TemplateResponse(request, template, ctx)


@router.get("/models", dependencies=[Depends(_feature_public_site)])
async def page_models(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    return await _render_marketing(request, "/models", session, signed_in=customer is not None)


@router.get("/pricing", dependencies=[Depends(_feature_public_site)])
async def page_pricing(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    return await _render_marketing(request, "/pricing", session, signed_in=customer is not None)


@router.get("/product/{slug}", dependencies=[Depends(_feature_public_site)])
async def page_product(
    slug: str,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    path = f"/product/{slug}"
    if path not in MARKETING_INDEX:
        raise HTTPException(status_code=404)
    return await _render_marketing(request, path, session, signed_in=customer is not None)


@router.get("/solutions/{slug}", dependencies=[Depends(_feature_public_site)])
async def page_solutions(
    slug: str,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
):
    path = f"/solutions/{slug}"
    if path not in MARKETING_INDEX:
        raise HTTPException(status_code=404)
    return await _render_marketing(request, path, session, signed_in=customer is not None)
