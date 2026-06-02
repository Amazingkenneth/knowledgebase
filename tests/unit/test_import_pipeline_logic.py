"""Regression tests for ImportPipeline control-flow fixes.

These cover the audit findings without touching Elasticsearch or optional
extraction libraries — the pipeline's ES/embedder/taxonomy collaborators are
stubbed so we exercise pure orchestration logic:

* commit_session keeps going after a bad document and reports honest status (A1)
* session TTL eviction bounds the in-memory session store (A3)
* server-side folder scans are confined to ingest.scan_root (B1)
* the document-index range error is a 400, not a 404 (B4)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from kb.config import Settings
from kb.models.ingest import ImportSession, ImportStatus, StagedDocument
from kb.models.taxonomy import KnowledgeType
from kb.services import import_pipeline as ip
from kb.services.indexing import IndexingError


def _staged(index: int, title: str) -> StagedDocument:
    return StagedDocument(
        index=index,
        knowledge_type=KnowledgeType.ALARM,
        title=title,
        source_file="f.pptx",
        accepted=True,
    )


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> ip.ImportPipeline:
    """A pipeline whose per-document commit helpers are neutralised so commit
    flow can be driven purely by which titles we mark as "BAD"."""
    es = SimpleNamespace(index=AsyncMock())
    embedder = SimpleNamespace(embed=AsyncMock(return_value=[[0.0], [0.0]]))
    pipeline = ip.ImportPipeline(
        es=es, settings=Settings(), embedder=embedder, taxonomy=SimpleNamespace(),
    )

    monkeypatch.setattr(ip, "_staged_to_knowledge_doc", lambda staged: staged)
    monkeypatch.setattr(ip, "build_title_text", lambda doc: "t")
    monkeypatch.setattr(ip, "build_body", lambda doc: "b")
    monkeypatch.setattr(ip, "doc_id", lambda doc: f"id-{doc.index}")
    monkeypatch.setattr(ip, "_to_es_source", lambda doc, tv, bv: {})
    monkeypatch.setattr(ip, "alias_name", lambda prefix, kt: "kb_alarm")

    def _validate(doc, taxonomy):
        if doc.title == "BAD":
            raise IndexingError("project 'X' not in taxonomy")

    monkeypatch.setattr(ip, "validate_against_taxonomy", _validate)
    return pipeline


# ── A1: commit loop no longer drops documents after the first failure ─────────

async def test_commit_continues_past_bad_document(stub_pipeline: ip.ImportPipeline):
    session = ImportSession(
        session_id="s1",
        documents=[_staged(0, "ok-a"), _staged(1, "BAD"), _staged(2, "ok-b")],
        created_at=datetime.now(UTC),
    )
    stub_pipeline._sessions["s1"] = session

    result = await stub_pipeline.commit_session("s1")

    # Both good docs commit even though the middle one failed.
    assert result["committed"] == 2
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 1
    # The doc *after* the failure was still indexed — not silently dropped.
    assert stub_pipeline._es.index.await_count == 2
    # Partial success keeps COMMITTED but carries the error list.
    assert session.status == ImportStatus.COMMITTED


async def test_commit_all_failed_marks_session_failed(stub_pipeline: ip.ImportPipeline):
    session = ImportSession(
        session_id="s2",
        documents=[_staged(0, "BAD"), _staged(1, "BAD")],
        created_at=datetime.now(UTC),
    )
    stub_pipeline._sessions["s2"] = session

    result = await stub_pipeline.commit_session("s2")

    assert result["committed"] == 0
    assert len(result["errors"]) == 2
    assert stub_pipeline._es.index.await_count == 0
    assert session.status == ImportStatus.FAILED


async def test_commit_clean_run_is_committed(stub_pipeline: ip.ImportPipeline):
    session = ImportSession(
        session_id="s3",
        documents=[_staged(0, "ok-a"), _staged(1, "ok-b")],
        created_at=datetime.now(UTC),
    )
    stub_pipeline._sessions["s3"] = session

    result = await stub_pipeline.commit_session("s3")

    assert result["committed"] == 2
    assert result["errors"] == []
    assert session.status == ImportStatus.COMMITTED


# ── A3: session TTL eviction ──────────────────────────────────────────────────

def test_evict_drops_old_terminal_sessions_only(stub_pipeline: ip.ImportPipeline):
    ttl = stub_pipeline._settings.ingest.session_ttl_minutes
    old = datetime.now(UTC) - timedelta(minutes=ttl + 10)
    fresh = datetime.now(UTC)

    def _sess(sid, status, when):
        return ImportSession(session_id=sid, status=status, created_at=when)

    stub_pipeline._sessions = {
        "old_committed": _sess("old_committed", ImportStatus.COMMITTED, old),
        "old_failed": _sess("old_failed", ImportStatus.FAILED, old),
        "old_extracting": _sess("old_extracting", ImportStatus.EXTRACTING, old),
        "old_ready": _sess("old_ready", ImportStatus.READY, old),
        "fresh_committed": _sess("fresh_committed", ImportStatus.COMMITTED, fresh),
    }

    stub_pipeline._evict_expired_sessions()

    remaining = set(stub_pipeline._sessions)
    # Terminal + expired are gone.
    assert "old_committed" not in remaining
    assert "old_failed" not in remaining
    # In-flight / under-review sessions are never evicted out from under a user.
    assert "old_extracting" in remaining
    assert "old_ready" in remaining
    # Recent terminal session survives.
    assert "fresh_committed" in remaining


# ── B1: folder scan confined to scan_root ─────────────────────────────────────

async def test_scan_rejects_folder_outside_root(tmp_path):
    settings = Settings()
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    settings.ingest.scan_root = str(root)
    pipeline = ip.ImportPipeline(
        es=SimpleNamespace(), settings=settings,
        embedder=SimpleNamespace(), taxonomy=SimpleNamespace(),
    )

    with pytest.raises(ValueError, match="allowed scan root"):
        await pipeline.start_scan(str(outside))


async def test_scan_inside_root_passes_guard(tmp_path):
    """A path inside the root clears the guard (and fails later only because the
    folder has no supported files — proving the guard itself allowed it)."""
    settings = Settings()
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    settings.ingest.scan_root = str(root)
    pipeline = ip.ImportPipeline(
        es=SimpleNamespace(), settings=settings,
        embedder=SimpleNamespace(), taxonomy=SimpleNamespace(),
    )

    with pytest.raises(ValueError, match="No supported files"):
        await pipeline.start_scan(str(sub))


async def test_scan_missing_folder_raises(tmp_path):
    settings = Settings()
    settings.ingest.scan_root = str(tmp_path)
    pipeline = ip.ImportPipeline(
        es=SimpleNamespace(), settings=settings,
        embedder=SimpleNamespace(), taxonomy=SimpleNamespace(),
    )

    with pytest.raises(ValueError, match="Folder not found"):
        await pipeline.start_scan(str(tmp_path / "does-not-exist"))


# ── B4: document index range error is a 400 ───────────────────────────────────

async def test_document_index_out_of_range_is_400():
    from kb.api.ingest import update_document
    from kb.models.ingest import DocumentUpdate

    session = ImportSession(session_id="s1", documents=[])
    pipeline = SimpleNamespace(get_session=lambda sid: session)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(import_pipeline=pipeline))
    )

    with pytest.raises(HTTPException) as exc_info:
        await update_document(request, "s1", 5, DocumentUpdate())
    assert exc_info.value.status_code == 400
