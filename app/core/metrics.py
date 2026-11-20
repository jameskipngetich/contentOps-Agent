"""Prometheus metrics.

Labels are deliberately low-cardinality: routes are logged by their template
path (``/api/v1/reports/{report_id}``) rather than the raw URL so ids cannot
create an unbounded number of series.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info

REQUESTS_TOTAL = Counter(
    "contentops_http_requests_total",
    "HTTP requests handled by the service.",
    ["method", "path", "status"],
)

REQUEST_DURATION = Histogram(
    "contentops_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

EVALUATIONS_TOTAL = Counter(
    "contentops_evaluations_total",
    "Evaluation requests by final status.",
    ["status"],
)

EVALUATION_DURATION = Histogram(
    "contentops_evaluation_duration_seconds",
    "Time spent evaluating a single content set.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

VARIANT_RESULTS_TOTAL = Counter(
    "contentops_variant_results_total",
    "Variant outcomes produced by the evaluation pipeline.",
    ["platform", "status"],
)

FAILED_CHECKS_TOTAL = Counter(
    "contentops_failed_checks_total",
    "Individual checks that did not pass.",
    ["platform", "check", "status"],
)

INTEGRATION_REQUESTS_TOTAL = Counter(
    "contentops_integration_requests_total",
    "Calls made to external content services.",
    ["service", "operation", "outcome"],
)

INTEGRATION_LATENCY = Histogram(
    "contentops_integration_latency_seconds",
    "Latency of external service calls.",
    ["service", "operation"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

REPORTS_STORED = Gauge(
    "contentops_reports_stored",
    "Number of evaluation reports currently stored.",
)

SEMANTIC_EVALUATOR_USED = Counter(
    "contentops_semantic_evaluator_total",
    "Which semantic evaluator produced results.",
    ["evaluator"],
)

APP_INFO = Info("contentops_app", "Build information for the running service.")


def set_app_info(version: str, environment: str) -> None:
    APP_INFO.info({"version": version, "environment": environment})
