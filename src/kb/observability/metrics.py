"""Prometheus metrics shared across the app.

The collectors are module-level singletons so any module can record into them
without threading a registry through call sites. ``record_upstream_error`` is
the one helper services call to flag a dependency failure (LLM / embedding /
Elasticsearch); HTTP request metrics are recorded by the middleware.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

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
UPSTREAM_LATENCY = Histogram(
    "kb_upstream_latency_seconds",
    "Latency of calls to an upstream dependency.",
    ["service"],  # "llm" | "embedding" | "es"
)
IMPORT_FILES = Counter(
    "kb_import_files_total",
    "Files handled by the import pipeline, by terminal status.",
    ["status"],  # "done" | "failed" | "skipped_duplicate" | "unsupported"
)
IMPORT_DOCS = Counter(
    "kb_import_docs_total",
    "Staged documents by outcome.",
    ["outcome"],  # "extracted" | "committed" | "rejected"
)
IMPORT_COMMIT_DURATION = Histogram(
    "kb_import_commit_duration_seconds",
    "Wall-clock time to commit an import session to Elasticsearch.",
)


def record_upstream_error(service: str) -> None:
    """Increment the upstream-error counter for ``service`` (llm/embedding/es)."""
    UPSTREAM_ERRORS.labels(service=service).inc()


def record_import_file(status: str) -> None:
    """Count one file reaching a terminal import status."""
    IMPORT_FILES.labels(status=status).inc()


def record_import_docs(outcome: str, count: int) -> None:
    """Count ``count`` staged documents with the given outcome."""
    if count:
        IMPORT_DOCS.labels(outcome=outcome).inc(count)


@contextmanager
def measure_upstream(service: str) -> Iterator[None]:
    """Observe wall-clock latency of an upstream call into ``UPSTREAM_LATENCY``.

    Usable around an ``await`` — the timer stops when the ``with`` block exits,
    whether the call returned or raised::

        with metrics.measure_upstream("llm"):
            await client.complete(...)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        UPSTREAM_LATENCY.labels(service=service).observe(time.perf_counter() - start)


def render() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics response."""
    return generate_latest(), CONTENT_TYPE_LATEST
