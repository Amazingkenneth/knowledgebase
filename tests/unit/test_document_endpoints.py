"""HTTP-level tests for the documents router + a direct test of the stats
aggregation (the search-box autocomplete data source).

The IndexingService collaborator is stubbed so we exercise the routing/parse/
error-mapping layer without Elasticsearch.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.api import documents
from kb.config import Settings
from kb.services.indexing import IndexingError


@pytest.fixture
def client_and_indexing() -> tuple[TestClient, SimpleNamespace]:
    indexing = SimpleNamespace(
        refresh_taxonomy=lambda taxonomy: None,
        index_one=AsyncMock(return_value="alarm-1"),
        index_bulk=AsyncMock(return_value={"indexed": 1, "errors": []}),
        delete=AsyncMock(return_value=True),
    )
    app = FastAPI()
    app.state.settings = Settings()
    app.state.indexing = indexing
    app.state.taxonomy_store = SimpleNamespace(current=SimpleNamespace())
    app.include_router(documents.router)
    return TestClient(app), indexing


_VALID_ALARM = {
    "title": "E4000 温度告警",
    "content": "报警内容",
    "resolution": "更换传感器",
}


def test_index_one_valid_is_201(client_and_indexing):
    tc, _ = client_and_indexing
    resp = tc.post("/api/v1/documents/alarm", json=_VALID_ALARM)
    assert resp.status_code == 201
    assert resp.json() == {"id": "alarm-1"}


def test_index_one_missing_required_is_400(client_and_indexing):
    tc, _ = client_and_indexing
    # No content/resolution → pydantic validation fails before indexing.
    resp = tc.post("/api/v1/documents/alarm", json={"title": "只有标题"})
    assert resp.status_code == 400


def test_index_one_taxonomy_rejection_is_400(client_and_indexing):
    tc, indexing = client_and_indexing
    indexing.index_one = AsyncMock(side_effect=IndexingError("project 'X' not in taxonomy"))
    resp = tc.post("/api/v1/documents/alarm", json=_VALID_ALARM)
    assert resp.status_code == 400


def test_bulk_parse_error_short_circuits(client_and_indexing):
    tc, indexing = client_and_indexing
    resp = tc.post(
        "/api/v1/documents/alarm/_bulk",
        json=[_VALID_ALARM, {"title": "bad — missing fields"}],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["indexed"] == 0
    assert body["errors"] and body["errors"][0]["row"] == 1
    # A partial bulk must NOT run when any row fails to parse.
    indexing.index_bulk.assert_not_awaited()


def test_delete_missing_is_404(client_and_indexing):
    tc, indexing = client_and_indexing
    indexing.delete = AsyncMock(return_value=False)
    resp = tc.delete("/api/v1/documents/alarm/does-not-exist")
    assert resp.status_code == 404


def test_delete_found_is_204(client_and_indexing):
    tc, _ = client_and_indexing
    resp = tc.delete("/api/v1/documents/alarm/alarm-1")
    assert resp.status_code == 204


# ── C1: stats aggregation merges error codes across indices ───────────────────

async def test_doc_stats_includes_error_codes():
    """The new by_error_code aggregation is merged across every knowledge-type
    index — this is what feeds the search-box autocomplete."""
    def _resp(error_buckets):
        return {
            "hits": {"total": {"value": sum(b["doc_count"] for b in error_buckets)}},
            "aggregations": {
                "project": {"buckets": []},
                "equipment": {"buckets": []},
                "error_code": {"buckets": error_buckets},
            },
        }

    # Different indices contribute overlapping codes; counts must sum.
    responses = iter([
        _resp([{"key": "E4000", "doc_count": 3}]),
        _resp([{"key": "E4000", "doc_count": 2}, {"key": "E5001", "doc_count": 1}]),
        _resp([]),
    ])
    es = SimpleNamespace(search=AsyncMock(side_effect=lambda **kw: next(responses)))

    out = await documents.doc_stats(es, Settings())

    assert out["by_error_code"]["E4000"] == 5
    assert out["by_error_code"]["E5001"] == 1
