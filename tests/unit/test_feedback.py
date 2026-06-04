"""Tests for the search-feedback router (👍/👎 capture + admin aggregate)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.api import deps, feedback


def _app(es) -> TestClient:
    app = FastAPI()
    app.include_router(feedback.router)
    app.dependency_overrides[deps._es] = lambda: es
    return TestClient(app)


def test_submit_feedback_records_and_returns_202():
    es = SimpleNamespace(index=AsyncMock())
    tc = _app(es)
    resp = tc.post(
        "/api/v1/search/feedback",
        json={"doc_id": "alarm-1", "helpful": False, "query_text": "E4000"},
    )
    assert resp.status_code == 202
    es.index.assert_awaited_once()
    # The boolean and query are persisted for later aggregation.
    doc = es.index.await_args.kwargs["document"]
    assert doc["doc_id"] == "alarm-1"
    assert doc["helpful"] is False
    assert doc["query_text"] == "E4000"


def test_submit_feedback_requires_doc_id():
    es = SimpleNamespace(index=AsyncMock())
    tc = _app(es)
    resp = tc.post("/api/v1/search/feedback", json={"helpful": True})
    assert resp.status_code == 422


def test_submit_feedback_storage_failure_is_503():
    es = SimpleNamespace(index=AsyncMock(side_effect=RuntimeError("es down")))
    tc = _app(es)
    resp = tc.post(
        "/api/v1/search/feedback",
        json={"doc_id": "d1", "helpful": True},
    )
    assert resp.status_code == 503


def test_feedback_summary_computes_ratio_and_top_queries():
    agg = {
        "aggregations": {
            "helpful": {"buckets": [
                {"key": 1, "key_as_string": "true", "doc_count": 7},
                {"key": 0, "key_as_string": "false", "doc_count": 3},
            ]},
            "unhelpful_queries": {
                "by_query": {"buckets": [
                    {"key": "E4000 温度", "doc_count": 2},
                    {"key": "对准失败", "doc_count": 1},
                ]},
            },
        },
    }
    es = SimpleNamespace(search=AsyncMock(return_value=agg))
    tc = _app(es)
    resp = tc.get("/api/v1/admin/search-feedback")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 10
    assert body["helpful"] == 7
    assert body["unhelpful"] == 3
    assert body["helpful_ratio"] == pytest.approx(0.7)
    assert body["top_unhelpful_queries"][0] == {"query": "E4000 温度", "unhelpful": 2}
