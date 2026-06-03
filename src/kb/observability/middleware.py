"""ASGI middleware: request-id propagation and Prometheus request metrics."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from kb.observability import metrics
from kb.observability.logging_config import request_id_var

_Next = Callable[[Request], Awaitable[Response]]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a request id to the logging context and echo it on the response."""

    async def dispatch(self, request: Request, call_next: _Next) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        # Stash on scope-backed state too, so exception handlers running in the
        # outer ServerErrorMiddleware (after the contextvar is reset below) can
        # still recover the id for the error body.
        request.state.request_id = rid
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record request count, latency, and in-flight gauge per route template."""

    async def dispatch(self, request: Request, call_next: _Next) -> Response:
        metrics.HTTP_IN_PROGRESS.inc()
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            metrics.HTTP_IN_PROGRESS.dec()
            # Use the matched route template (e.g. /api/v1/ingest/sessions/{id})
            # rather than the raw path, so per-session ids don't explode label
            # cardinality. Falls back to the raw path when nothing matched.
            route = request.scope.get("route")
            path = getattr(route, "path", None) or request.url.path
            elapsed = time.perf_counter() - start
            metrics.HTTP_REQUESTS.labels(request.method, path, str(status_code)).inc()
            metrics.HTTP_DURATION.labels(request.method, path).observe(elapsed)
