"""HTTP middleware: request ids, access logs and Prometheus instrumentation."""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger, request_id_ctx
from app.core.metrics import REQUEST_DURATION, REQUESTS_TOTAL

logger = get_logger("contentops.access")

# Paths that should not appear in request metrics or access logs.
_EXCLUDED_PATHS = {"/metrics"}


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id")
        request_id = (incoming or uuid.uuid4().hex)[:64]
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            route = request.scope.get("route")
            path_template = getattr(route, "path", None) or request.url.path

            if request.url.path not in _EXCLUDED_PATHS:
                REQUESTS_TOTAL.labels(
                    method=request.method,
                    path=path_template,
                    status=str(status_code),
                ).inc()
                REQUEST_DURATION.labels(method=request.method, path=path_template).observe(
                    duration_ms / 1000
                )

            if request.url.path not in _EXCLUDED_PATHS:
                logger.info(
                    "request completed",
                    extra={
                        "method": request.method,
                        "path": path_template,
                        "status_code": status_code,
                        "duration_ms": duration_ms,
                        "client": request.client.host if request.client else None,
                    },
                )
            request_id_ctx.reset(token)
