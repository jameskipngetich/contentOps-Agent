"""HTTP-level tests for the public API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.integrations.variant_generator import MockVariantGeneratorAdapter
from tests.conftest import GOOD_VARIANTS, SOURCE

EVALUATE = "/api/v1/evaluate"


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"source": dict(SOURCE), "variants": dict(GOOD_VARIANTS)}
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Service metadata and probes
# ---------------------------------------------------------------------------
def test_service_info(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "contentops-agent"
    assert "POST /api/v1/evaluate" in body["endpoints"]
    assert set(body["supported_platforms"]) == {"x", "instagram", "discord", "telegram"}


def test_health_is_liveness_only(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["uptime_seconds"] >= 0


def test_ready_reports_required_and_optional_dependencies(client: TestClient):
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    by_name = {check["name"]: check for check in body["checks"]}
    assert by_name["sqlite"]["required"] is True
    assert by_name["variant_generator"]["required"] is False
    assert by_name["llm"]["status"] == "degraded"


def test_ready_returns_503_when_the_database_is_unusable(client: TestClient):
    client.app.state.repository.healthcheck = lambda: (False, "unable to open database file")

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    sqlite_check = next(c for c in body["checks"] if c["name"] == "sqlite")
    assert sqlite_check["status"] == "unavailable"
    assert sqlite_check["detail"] == "unable to open database file"
    # Liveness must stay green: the process is alive, it just cannot serve.
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------
def test_evaluate_returns_201_and_location(client: TestClient, evaluate_payload):
    response = client.post(EVALUATE, json=evaluate_payload)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "passed"
    assert body["score"] == 100
    assert body["report_id"] in response.headers["location"]
    assert response.headers["x-request-id"]


def test_evaluate_reports_missing_platform_field(client: TestClient):
    variants = dict(GOOD_VARIANTS)
    variants.pop("instagram_caption")
    response = client.post(EVALUATE, json=_payload(variants=variants))
    assert response.status_code == 201
    body = response.json()
    instagram = next(v for v in body["variants"] if v["platform"] == "instagram")
    assert instagram["status"] == "failed"
    assert body["status"] == "failed"
    assert body["failed_checks"] >= 1
    assert body["regeneration_requests"][0]["platform"] == "instagram"


def test_evaluate_fetches_variants_when_omitted(client: TestClient):
    response = client.post(EVALUATE, json={"source": dict(SOURCE)})
    assert response.status_code == 201
    assert response.json()["variant_source"] == "variant_generator"
    assert len(response.json()["variants"]) == 4


def test_evaluate_platform_subset(client: TestClient):
    response = client.post(EVALUATE, json=_payload(platforms=["x"]))
    assert response.status_code == 201
    assert [v["platform"] for v in response.json()["variants"]] == ["x"]


def test_evaluate_accepts_studio_post_name(client: TestClient):
    response = client.post(
        EVALUATE, json={"studio_post_name": "demo-post", "variants": dict(GOOD_VARIANTS)}
    )
    assert response.status_code == 201
    assert response.json()["source"]["title"].startswith("Shipping a content pipeline")


def test_evaluate_unknown_studio_post_returns_404(client: TestClient):
    response = client.post(EVALUATE, json={"studio_post_name": "no-such-post"})
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["request_id"]


def test_evaluate_upstream_failure_returns_502(client_factory):
    client = client_factory(mock_variant_scenario="generator_error")
    response = client.post(EVALUATE, json={"source": dict(SOURCE)})
    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "upstream_unavailable"
    assert body["error"]["details"]["service"] == "variant_generator"


def test_evaluate_overflow_scenario_marks_x_as_failed(client_factory):
    client = client_factory(mock_variant_scenario="overflow")
    response = client.post(EVALUATE, json={"source": dict(SOURCE)})
    assert response.status_code == 201
    x_variant = next(v for v in response.json()["variants"] if v["platform"] == "x")
    assert x_variant["status"] == "failed"
    assert any(check["name"] == "length" for check in x_variant["checks"])


def test_duplicate_scenario_flags_cross_posting(client_factory):
    client = client_factory(mock_variant_scenario="duplicate")
    response = client.post(EVALUATE, json={"source": dict(SOURCE)})
    assert response.status_code == 201
    assert response.json()["duplicates"]
    assert response.json()["failed_checks"] >= 4


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"source": dict(SOURCE), "studio_post_name": "demo-post"},
        {"source": {**SOURCE, "title": ""}},
        {"source": dict(SOURCE), "platforms": ["tiktok"]},
        {"source": dict(SOURCE), "variants": {"tweet": "unknown field"}},
        {"source": dict(SOURCE), "unexpected": True},
        {"source": {**SOURCE, "url": "not-a-url"}},
    ],
)
def test_invalid_requests_return_422_envelope(client: TestClient, body: dict[str, object]):
    response = client.post(EVALUATE, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def test_reports_list_and_fetch(client: TestClient, evaluate_payload):
    created = client.post(EVALUATE, json=evaluate_payload).json()

    listing = client.get("/api/v1/reports")
    assert listing.status_code == 200
    assert listing.json()["count"] == 1
    assert listing.json()["items"][0]["report_id"] == created["report_id"]

    detail = client.get(f"/api/v1/reports/{created['report_id']}")
    assert detail.status_code == 200
    assert detail.json()["report_id"] == created["report_id"]


def test_reports_filters(client: TestClient, evaluate_payload):
    client.post(EVALUATE, json=evaluate_payload)
    client.post(EVALUATE, json=_payload(variants={"x_post": ""}))

    failed = client.get("/api/v1/reports", params={"status": "failed"})
    assert failed.status_code == 200
    assert failed.json()["count"] == 1

    by_platform = client.get("/api/v1/reports", params={"platform": "x"})
    assert by_platform.json()["count"] == 2

    unfiltered = client.get("/api/v1/reports", params={"platform": "instagram"})
    assert unfiltered.json()["count"] == 2

    limited = client.get("/api/v1/reports", params={"limit": 1})
    assert len(limited.json()["items"]) == 1


def test_reports_invalid_query_returns_422(client: TestClient):
    assert client.get("/api/v1/reports", params={"limit": 500}).status_code == 422
    assert client.get("/api/v1/reports", params={"status": "unknown"}).status_code == 422


def test_report_not_found(client: TestClient):
    response = client.get("/api/v1/reports/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Regeneration
# ---------------------------------------------------------------------------
def test_regenerate_endpoint_returns_child_report(client_factory):
    client = client_factory(mock_variant_scenario="missing_field")
    original = client.post(EVALUATE, json={"source": dict(SOURCE)}).json()
    assert original["status"] == "failed"

    # Swap the adapter for one that produces complete content, which is what a
    # healthy generator would do after the regeneration request.
    client.app.state.agent.variant_generator = MockVariantGeneratorAdapter("clean")

    response = client.post(f"/api/v1/reports/{original['report_id']}/regenerate")
    assert response.status_code == 201
    body = response.json()
    assert body["parent_report_id"] == original["report_id"]
    assert [v["platform"] for v in body["variants"]] == ["instagram"]
    assert body["status"] == "passed"
    assert response.headers["location"].endswith(body["report_id"])


def test_regenerate_conflicts_when_nothing_failed(client: TestClient, evaluate_payload):
    created = client.post(EVALUATE, json=evaluate_payload).json()
    response = client.post(f"/api/v1/reports/{created['report_id']}/regenerate")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_regenerate_unknown_report_returns_404(client: TestClient):
    assert client.post("/api/v1/reports/nope/regenerate").status_code == 404


# ---------------------------------------------------------------------------
# Metrics and error shapes
# ---------------------------------------------------------------------------
def test_metrics_exposes_application_series(client: TestClient, evaluate_payload):
    client.post(EVALUATE, json=evaluate_payload)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    for metric in (
        "contentops_http_requests_total",
        "contentops_http_request_duration_seconds",
        "contentops_evaluations_total",
        "contentops_variant_results_total",
        "contentops_integration_requests_total",
    ):
        assert metric in body
    assert 'path="/api/v1/evaluate"' in body


def test_metrics_can_be_disabled(client_factory):
    client = client_factory(metrics_enabled=False)
    response = client.get("/metrics")
    assert response.status_code == 404


def test_unknown_route_uses_error_envelope(client: TestClient):
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_wrong_method_returns_405_envelope(client: TestClient):
    response = client.get(EVALUATE)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_request_id_header_is_echoed(client: TestClient):
    response = client.get("/health", headers={"x-request-id": "trace-123"})
    assert response.headers["x-request-id"] == "trace-123"


def test_openapi_schema_is_served(client: TestClient):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "ContentOps Agent"
    assert "/api/v1/evaluate" in schema["paths"]
