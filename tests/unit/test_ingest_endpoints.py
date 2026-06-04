"""HTTP-level tests for the ingest router.

Covers the routing/error-mapping layer that service-level tests don't reach:
404-vs-410 for missing sessions, bulk accept/retry, and durability recovery.
A real ImportPipeline (with stubbed ES/embedder/taxonomy collaborators) is wired
into ``app.state`` so the actual session_state()/accept_all() logic runs end to
end through FastAPI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.api import ingest
from kb.config import Settings
from kb.models.ingest import FileInfo, FileStatus, ImportSession, ImportStatus, StagedDocument
from kb.models.taxonomy import KnowledgeType
from kb.services import import_pipeline as ip


def _staged(index: int, title: str, kt: KnowledgeType = KnowledgeType.ALARM) -> StagedDocument:
    return StagedDocument(
        index=index, knowledge_type=kt, title=title, source_file="f.pptx", accepted=False,
    )


@pytest.fixture
def client() -> tuple[TestClient, ip.ImportPipeline]:
    pipeline = ip.ImportPipeline(
        es=SimpleNamespace(), settings=Settings(),
        embedder=SimpleNamespace(), taxonomy=SimpleNamespace(),
    )
    app = FastAPI()
    app.state.import_pipeline = pipeline
    app.include_router(ingest.router)
    return TestClient(app), pipeline


def test_get_unknown_session_is_404(client):
    tc, _ = client
    resp = tc.get("/api/v1/ingest/sessions/nope")
    assert resp.status_code == 404


def test_get_expired_session_is_410(client):
    """Regression guard for the session_state() crash: an evicted session must
    return 410 'please re-upload', not a 500 or a bare 404."""
    tc, pipeline = client
    old = datetime.now(UTC) - timedelta(
        minutes=pipeline._settings.ingest.session_hard_ttl_minutes + 10
    )
    pipeline._sessions["gone"] = ImportSession(
        session_id="gone", status=ImportStatus.READY, created_at=old,
    )
    pipeline._evict_expired_sessions()

    resp = tc.get("/api/v1/ingest/sessions/gone")
    assert resp.status_code == 410
    assert "re-upload" in resp.json()["detail"].lower()


def test_get_live_session_returns_shape(client):
    tc, pipeline = client
    pipeline._sessions["s"] = ImportSession(
        session_id="s", status=ImportStatus.READY,
        files=[FileInfo(file_name="a.pdf", file_hash="h", file_type="pdf",
                        status=FileStatus.DONE)],
        documents=[_staged(0, "doc")],
    )
    resp = tc.get("/api/v1/ingest/sessions/s")
    assert resp.status_code == 200
    body = resp.json()
    assert body["files_total"] == 1
    assert body["files_processed"] == 1
    assert len(body["documents"]) == 1


def test_accept_reject_out_of_range_is_400(client):
    tc, pipeline = client
    pipeline._sessions["s"] = ImportSession(session_id="s", documents=[])
    resp = tc.patch("/api/v1/ingest/sessions/s/documents/5", json={"accepted": True})
    assert resp.status_code == 400


def test_accept_all_accepts_every_doc(client):
    tc, pipeline = client
    pipeline._sessions["s"] = ImportSession(
        session_id="s", documents=[_staged(0, "a"), _staged(1, "b")],
    )
    resp = tc.post("/api/v1/ingest/sessions/s/documents/accept-all", json={})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 2}
    assert all(d.accepted for d in pipeline._sessions["s"].documents)


def test_accept_all_filtered_by_type(client):
    tc, pipeline = client
    pipeline._sessions["s"] = ImportSession(
        session_id="s",
        documents=[_staged(0, "a", KnowledgeType.ALARM), _staged(1, "b", KnowledgeType.SETUP)],
    )
    resp = tc.post(
        "/api/v1/ingest/sessions/s/documents/accept-all",
        json={"knowledge_type": "alarm"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}


def test_retry_failed_with_no_failures_is_202(client):
    tc, pipeline = client
    pipeline._sessions["s"] = ImportSession(
        session_id="s", status=ImportStatus.READY,
        files=[FileInfo(file_name="a.pdf", file_hash="h", file_type="pdf",
                        status=FileStatus.DONE)],
    )
    resp = tc.post("/api/v1/ingest/sessions/s/retry-failed", json={})
    assert resp.status_code == 202


def test_recommit_tracking_nothing_pending(client):
    tc, pipeline = client
    pipeline._sessions["s"] = ImportSession(session_id="s", status=ImportStatus.COMMITTED)
    resp = tc.post("/api/v1/ingest/sessions/s/recommit-tracking")
    assert resp.status_code == 200
    assert resp.json()["recovered"] == 0


def test_recommit_tracking_unknown_session_404(client):
    tc, _ = client
    resp = tc.post("/api/v1/ingest/sessions/missing/recommit-tracking")
    assert resp.status_code == 404
