"""Prometheus metrics shared across the app.

The collectors are module-level singletons so any module can record into them
without threading a registry through call sites. ``record_upstream_error`` is
the one helper services call to flag a dependency failure (LLM / embedding /
Elasticsearch); HTTP request metrics are recorded by the middleware.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "kb_http_requests_total",
    "Total HTTP requests handled.",
    ["method", "path", "status"],
)
HTTP_DURATION = Histogram(
    "kb_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
)
HTTP_IN_PROGRESS = Gauge(
    "kb_http_requests_in_progress",
    "HTTP requests currently being served.",
)
UPSTREAM_ERRORS = Counter(
    "kb_upstream_errors_total",
    "Errors talking to an upstream dependency.",
    ["service"],  # "llm" | "embedding" | "es"
)


def record_upstream_error(service: str) -> None:
    """Increment the upstream-error counter for ``service`` (llm/embedding/es)."""
    UPSTREAM_ERRORS.labels(service=service).inc()


def render() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics response."""
    return generate_latest(), CONTENT_TYPE_LATEST
