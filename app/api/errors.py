"""Translate expected application failures at the HTTP boundary."""

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.errors import AccessDenied, DomainError, EntityNotFound, StateConflict


async def domain_error_response(request: Request, exc: Exception) -> JSONResponse:
    """Preserve the API's existing ``detail`` envelope for domain failures."""
    if not isinstance(exc, DomainError):
        raise exc
    status = 400
    if isinstance(exc, EntityNotFound):
        status = 404
    elif isinstance(exc, AccessDenied):
        status = 403
    elif isinstance(exc, StateConflict):
        status = 409
    return JSONResponse(status_code=status, content={"detail": exc.message})
