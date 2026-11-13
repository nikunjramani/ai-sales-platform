"""The answers every router shares: a denied permission, a bad value, a database or storage
outage. Each module's router maps only its own errors (its guarded helper)."""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from sales_platform.platform.domain.tenancy import PermissionDenied

logger = logging.getLogger(__name__)

UNAVAILABLE = "Temporarily unavailable; try again"


def add_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PermissionDenied)
    async def denied(_: Request, error: Exception) -> JSONResponse:
        detail = str(error) or "Not permitted"
        return JSONResponse({"detail": detail}, status_code=status.HTTP_403_FORBIDDEN)

    @app.exception_handler(ValueError)
    async def invalid(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            {"detail": str(error)}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )

    async def unavailable(request: Request, _: Exception) -> JSONResponse:
        logger.exception("Request failed: %s %s", request.method, request.url.path)
        code = status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse({"detail": UNAVAILABLE}, status_code=code)

    app.add_exception_handler(SQLAlchemyError, unavailable)
    app.add_exception_handler(OSError, unavailable)
