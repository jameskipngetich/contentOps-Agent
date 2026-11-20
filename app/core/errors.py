"""Domain errors and the HTTP error envelope."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, request_id_ctx

logger = get_logger(__name__)


class AppError(Exception):
    """Base class for expected, mapped application errors."""

    code = "internal_error"
    status_code = 500
    message = "Unexpected error."

    def __init__(self, message: str | None = None, *, details: Any = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details = details


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404
    message = "Resource not found."


class UpstreamServiceError(AppError):
    """An external service failed in a way the caller should know about."""

    code = "upstream_unavailable"
    status_code = 502
    message = "An upstream service could not provide a usable response."


class ConflictError(AppError):
    code = "conflict"
    status_code = 409
    message = "The request conflicts with the current state of the resource."


class ConfigurationError(AppError):
    code = "configuration_error"
    status_code = 500
    message = "The service is not configured to serve this request."


def error_payload(
    code: str, message: str, details: Any = None, request_id: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id or request_id_ctx.get(),
        }
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _new_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or request_id_ctx.get()


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers that always answer with the same error envelope."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "request failed",
            extra={
                "error_code": exc.code,
                "status_code": exc.status_code,
                "path": request.url.path,
                "detail": exc.message,
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.message, exc.details, _new_request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.info(
            "request validation failed",
            extra={"path": request.url.path, "errors": exc.errors()},
        )
        return JSONResponse(
            status_code=422,
            content=error_payload(
                "validation_error",
                "Request body or parameters failed validation.",
                _jsonable_errors(exc.errors()),
                _new_request_id(request),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(code, str(exc.detail), None, _new_request_id(request)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled error",
            extra={"path": request.url.path, "exc_type": type(exc).__name__},
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(
                "internal_error",
                "Internal server error. Use the request id to find the log entry.",
                None,
                _new_request_id(request),
            ),
        )


def _jsonable_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(errors)
