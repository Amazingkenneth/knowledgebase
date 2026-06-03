"""Global exception-handler contract.

The /search route delegates straight to the search service, so making the
service double raise lets us assert how each failure class is mapped:
  * Elasticsearch transport (unreachable)  -> 503
  * Elasticsearch API error (bad query)    -> 502
  * any other unforeseen error             -> 500
Each error body must carry the request id so logs and responses correlate.
"""

from __future__ import annotations

from elasticsearch import ApiError, TransportError
from tests.api.conftest import FakeSearch

_RID = "rid-abc-123"
_QUERY = {"keywords": ["pump"], "mode": "auto"}


class _FakeApiError(ApiError):
    """ApiError whose normal __init__ needs a transport meta we don't have."""

    def __init__(self, message: str) -> None:
        Exception.__init__(self, message)
        self._message = message

    def __str__(self) -> str:
        return self._message


def test_es_transport_error_maps_to_503(make_client):
    search = FakeSearch(error=TransportError("connection refused"))
    with make_client(search=search) as c:
        r = c.post("/api/v1/search", json=_QUERY, headers={"x-request-id": _RID})
    assert r.status_code == 503
    body = r.json()
    assert body["detail"] == "search backend unavailable"
    assert body["request_id"] == _RID
    assert r.headers["X-Request-ID"] == _RID


def test_es_api_error_maps_to_502(make_client):
    search = FakeSearch(error=_FakeApiError("search_phase_execution_exception"))
    with make_client(search=search) as c:
        r = c.post("/api/v1/search", json=_QUERY, headers={"x-request-id": _RID})
    assert r.status_code == 502
    body = r.json()
    assert body["detail"] == "search backend error"
    assert body["request_id"] == _RID


def test_unhandled_error_maps_to_500_with_request_id(make_client):
    search = FakeSearch(error=RuntimeError("boom"))
    # raise_server_exceptions=False so we observe the handler's 500 response
    # (produced in the outer ServerErrorMiddleware) rather than re-raising.
    with make_client(search=search, raise_server_exceptions=False) as c:
        r = c.post("/api/v1/search", json=_QUERY, headers={"x-request-id": _RID})
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "internal server error"
    # request_id survives into the outer handler via scope-backed request.state,
    # even though the logging contextvar has already been reset by then.
    assert body["request_id"] == _RID
