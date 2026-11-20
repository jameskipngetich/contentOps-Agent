"""Shared integration plumbing: errors, retries and timing.

Adapters never raise ``httpx`` exceptions to callers. They raise
``IntegrationError`` so the API layer can decide on an HTTP status and the
evaluation pipeline can record why an integration failed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

import httpx

from app.core.logging import get_logger
from app.core.metrics import INTEGRATION_LATENCY, INTEGRATION_REQUESTS_TOTAL

logger = get_logger(__name__)

ErrorKind = Literal[
    "timeout",
    "connection",
    "http_error",
    "malformed_response",
    "not_found",
    "disabled",
]


class IntegrationError(RuntimeError):
    def __init__(
        self,
        service: str,
        operation: str,
        message: str,
        *,
        kind: ErrorKind = "connection",
        status_code: int | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.service = service
        self.operation = operation
        self.kind = kind
        self.status_code = status_code
        self.attempts = attempts

    def as_detail(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "operation": self.operation,
            "kind": self.kind,
            "status_code": self.status_code,
            "attempts": self.attempts,
            "message": str(self),
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return super().__str__()


RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    operation: str,
    max_retries: int,
    backoff_base: float = 0.2,
    **kwargs: Any,
) -> httpx.Response:
    """Perform an HTTP request, retrying timeouts, connection errors and 5xx.

    Client errors (4xx other than 429) are not retried: they mean the request is
    wrong and repeating it will not help.
    """

    attempts = 0
    last_error: IntegrationError | None = None
    started = time.perf_counter()

    while attempts <= max_retries:
        attempts += 1
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            last_error = IntegrationError(
                service,
                operation,
                f"timed out after {client.timeout.read}s: {exc}",
                kind="timeout",
                attempts=attempts,
            )
        except httpx.HTTPError as exc:
            last_error = IntegrationError(
                service,
                operation,
                f"connection failed: {exc}",
                kind="connection",
                attempts=attempts,
            )
        else:
            if response.status_code < 400:
                INTEGRATION_REQUESTS_TOTAL.labels(
                    service=service, operation=operation, outcome="success"
                ).inc()
                INTEGRATION_LATENCY.labels(service=service, operation=operation).observe(
                    time.perf_counter() - started
                )
                return response

            retryable = response.status_code in RETRYABLE_STATUS
            last_error = IntegrationError(
                service,
                operation,
                f"upstream returned HTTP {response.status_code}",
                kind="http_error",
                status_code=response.status_code,
                attempts=attempts,
            )
            if not retryable:
                break

        INTEGRATION_REQUESTS_TOTAL.labels(
            service=service, operation=operation, outcome="retry"
        ).inc()
        logger.warning(
            "integration attempt failed",
            extra={
                "service": service,
                "attempt": attempts,
                "detail": str(last_error),
            },
        )
        if attempts <= max_retries:
            await asyncio.sleep(backoff_base * (2 ** (attempts - 1)))

    assert last_error is not None
    INTEGRATION_REQUESTS_TOTAL.labels(service=service, operation=operation, outcome="failure").inc()
    INTEGRATION_LATENCY.labels(service=service, operation=operation).observe(
        time.perf_counter() - started
    )
    logger.error(
        "integration failed",
        extra={
            "service": service,
            "attempt": last_error.attempts,
            "detail": str(last_error),
        },
    )
    raise last_error
