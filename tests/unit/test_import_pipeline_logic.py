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

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from kb.config import Settings
from kb.models.ingest import (
    DocumentUpdate,
    ExistingDocSnapshot,
    FileInfo,
    FileStatus,
    ImportSession,
    ImportStatus,
    StagedDocument,
)
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
    # embed is now called once with all (title, body) texts — return one vector
    # per input so the per-doc slicing lines up.
    embedder = SimpleNamespace(embed=AsyncMock(side_effect=lambda texts: [[0.0]] * len(texts)))
    pipeline = ip.ImportPipeline(
        es=es, settings=Settings(), embedder=embedder, taxonomy=SimpleNamespace(),
    )

    # Commit bulk-indexes in one request; fake async_bulk reports every action as
    # a success (count, no errors) and records calls for assertions.
    bulk_mock = AsyncMock(side_effect=lambda _es, actions, **kw: (len(actions), []))
    monkeypatch.setattr(ip, "async_bulk", bulk_mock)
    pipeline._bulk_mock = bulk_mock  # type: ignore[attr-defined]

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
    # Both good docs (the one before AND after the failure) went into a single
    # bulk request — not silently dropped.
    only_call = stub_pipeline._bulk_mock.await_args  # type: ignore[attr-defined]
    assert len(only_call.args[1]) == 2  # actions passed to async_bulk
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
    # Nothing valid to index → bulk is never called.
    assert stub_pipeline._bulk_mock.await_count == 0  # type: ignore[attr-defined]
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


# ── A3 (data loss): a tracker write failure after a successful index is surfaced ──

async def test_commit_surfaces_tracker_failure(stub_pipeline: ip.ImportPipeline):
    """Docs land in ES but the import-tracker row fails to update. They are
    searchable now yet would be dropped on the next reseed (restore_imports
    reads the tracker), so the response must flag tracking_failed + an error."""
    from unittest.mock import AsyncMock

    session = ImportSession(
        session_id="s-track",
        documents=[_staged(0, "ok-a"), _staged(1, "ok-b")],
        files=[FileInfo(
            file_name="f.pptx", file_hash="hash123", file_type="pptx",
            status=FileStatus.DONE,
        )],
        created_at=datetime.now(UTC),
    )
    stub_pipeline._sessions["s-track"] = session
    stub_pipeline._tracker.record_committed = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("tracker write failed")
    )

    result = await stub_pipeline.commit_session("s-track")

    assert result["committed"] == 2
    assert result["tracking_failed"] == 2
    assert any("won't survive" in e.get("hint", "") for e in result["errors"])
    # The docs DID index, so the session is still COMMITTED (not FAILED).
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


def test_evict_drops_abandoned_session_past_hard_ttl(stub_pipeline: ip.ImportPipeline):
    """A non-terminal (READY) session older than the hard TTL is evicted so an
    abandoned preview can't pin memory forever."""
    hard = stub_pipeline._settings.ingest.session_hard_ttl_minutes
    ancient = datetime.now(UTC) - timedelta(minutes=hard + 10)
    recent = datetime.now(UTC)

    stub_pipeline._sessions = {
        "ancient_ready": ImportSession(
            session_id="ancient_ready", status=ImportStatus.READY, created_at=ancient,
        ),
        "recent_ready": ImportSession(
            session_id="recent_ready", status=ImportStatus.READY, created_at=recent,
        ),
    }

    stub_pipeline._evict_expired_sessions()

    remaining = set(stub_pipeline._sessions)
    assert "ancient_ready" not in remaining  # hard TTL reclaims abandoned reviews
    assert "recent_ready" in remaining


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


# ── Single-file retry ─────────────────────────────────────────────────────────

def _bare_pipeline() -> ip.ImportPipeline:
    return ip.ImportPipeline(
        es=SimpleNamespace(), settings=Settings(),
        embedder=SimpleNamespace(), taxonomy=SimpleNamespace(),
    )


async def test_retry_file_reprocesses_in_place(monkeypatch, tmp_path):
    """A failed file is re-processed without touching the already-good file's
    docs; new docs are appended with indices continuing past the existing max."""
    pipeline = _bare_pipeline()
    on_disk = tmp_path / "scan.pdf"
    on_disk.write_bytes(b"x")
    pipeline._tracker.exists = AsyncMock(  # type: ignore[method-assign]
        return_value={"file_path": str(on_disk)}
    )

    good = FileInfo(file_name="ok.pdf", file_hash="h-ok", file_type="pdf", status=FileStatus.DONE)
    failed = FileInfo(
        file_name="scan.pdf", file_hash="h-scan", file_type="pdf",
        status=FileStatus.FAILED, message="image-only PDF",
    )
    existing = _staged(0, "from-ok")
    existing.source_file = "ok.pdf"
    session = ImportSession(
        session_id="s", status=ImportStatus.READY,
        files=[good, failed], documents=[existing], created_at=datetime.now(UTC),
    )
    pipeline._sessions["s"] = session

    async def fake_process_one(sess, info, path, kt, proj, equip, start_index, force_ocr=False):
        info.status = FileStatus.DONE
        info.message = "Extracted 1 documents."
        d = _staged(start_index, "recovered")
        d.source_file = info.file_name
        return [d]

    monkeypatch.setattr(pipeline, "_process_one", fake_process_one)

    returned = await pipeline.retry_file("s", "h-scan")
    assert returned is session
    await asyncio.gather(*pipeline._tasks)

    assert failed.status == FileStatus.DONE
    assert session.status == ImportStatus.READY
    titles = {d.title for d in session.documents}
    assert titles == {"from-ok", "recovered"}
    # The good file's doc is untouched; the recovered doc continues past index 0.
    recovered = next(d for d in session.documents if d.title == "recovered")
    assert recovered.index == 1


async def test_retry_file_forwards_force_ocr(monkeypatch, tmp_path):
    pipeline = _bare_pipeline()
    on_disk = tmp_path / "scan.pdf"
    on_disk.write_bytes(b"x")
    pipeline._tracker.exists = AsyncMock(return_value={"file_path": str(on_disk)})  # type: ignore[method-assign]
    failed = FileInfo(
        file_name="scan.pdf", file_hash="h-scan", file_type="pdf", status=FileStatus.FAILED,
    )
    session = ImportSession(session_id="s", status=ImportStatus.READY, files=[failed])
    pipeline._sessions["s"] = session

    seen: dict[str, bool] = {}

    async def fake_process_one(sess, info, path, kt, proj, equip, start_index, force_ocr=False):
        seen["force_ocr"] = force_ocr
        info.status = FileStatus.DONE
        return []

    monkeypatch.setattr(pipeline, "_process_one", fake_process_one)
    await pipeline.retry_file("s", "h-scan", force_ocr=True)
    await asyncio.gather(*pipeline._tasks)
    assert seen["force_ocr"] is True


async def test_retry_file_missing_on_disk_raises(tmp_path):
    pipeline = _bare_pipeline()
    pipeline._tracker.exists = AsyncMock(  # type: ignore[method-assign]
        return_value={"file_path": str(tmp_path / "gone.pdf")}
    )
    failed = FileInfo(
        file_name="scan.pdf", file_hash="h", file_type="pdf", status=FileStatus.FAILED,
    )
    pipeline._sessions["s"] = ImportSession(session_id="s", files=[failed])

    with pytest.raises(ValueError, match="no longer available"):
        await pipeline.retry_file("s", "h")


async def test_retry_file_unknown_file_raises():
    pipeline = _bare_pipeline()
    pipeline._sessions["s"] = ImportSession(session_id="s", files=[])
    with pytest.raises(ValueError, match="File not found"):
        await pipeline.retry_file("s", "nope")


# ── A1: session_state distinguishes an expired session from an unknown one ────

def test_session_state_reports_expired_after_eviction(stub_pipeline: ip.ImportPipeline):
    """The 410-vs-404 distinction the ingest router relies on: an id that was
    evicted by TTL must read back as 'expired', while a never-seen id is None."""
    ttl = stub_pipeline._settings.ingest.session_hard_ttl_minutes
    old = datetime.now(UTC) - timedelta(minutes=ttl + 10)
    stub_pipeline._sessions["gone"] = ImportSession(
        session_id="gone", status=ImportStatus.READY, created_at=old,
    )

    stub_pipeline._evict_expired_sessions()

    assert stub_pipeline.get_session("gone") is None
    assert stub_pipeline.session_state("gone") == "expired"
    assert stub_pipeline.session_state("never-existed") is None


def test_evicted_set_is_bounded(stub_pipeline: ip.ImportPipeline):
    """The evicted-id memory can't grow without bound."""
    cap = ip.ImportPipeline._EVICTED_CAP
    for i in range(cap + 50):
        stub_pipeline._remember_evicted(f"sid-{i}")
    assert len(stub_pipeline._evicted) == cap
    # Oldest were trimmed; newest survive.
    assert stub_pipeline.session_state("sid-0") is None
    assert stub_pipeline.session_state(f"sid-{cap + 49}") == "expired"


# ── B1: bulk accept ───────────────────────────────────────────────────────────

def test_accept_all_flips_every_doc(stub_pipeline: ip.ImportPipeline):
    docs = [_staged(0, "a"), _staged(1, "b"), _staged(2, "c")]
    for d in docs:
        d.accepted = False
    stub_pipeline._sessions["s"] = ImportSession(session_id="s", documents=docs)

    n = stub_pipeline.accept_all("s")

    assert n == 3
    assert all(d.accepted for d in docs)


def test_accept_all_filters_by_knowledge_type(stub_pipeline: ip.ImportPipeline):
    alarm = _staged(0, "alarm")
    setup = StagedDocument(
        index=1, knowledge_type=KnowledgeType.SETUP, title="setup",
        source_file="f.pptx", accepted=False,
    )
    alarm.accepted = False
    stub_pipeline._sessions["s"] = ImportSession(session_id="s", documents=[alarm, setup])

    n = stub_pipeline.accept_all("s", KnowledgeType.ALARM)

    assert n == 1
    assert alarm.accepted is True
    assert setup.accepted is False


# ── B1: bulk retry of all failed files ────────────────────────────────────────

async def test_retry_failed_files_reprocesses_only_failed(monkeypatch, tmp_path):
    pipeline = _bare_pipeline()
    on_disk = tmp_path / "scan.pdf"
    on_disk.write_bytes(b"x")
    pipeline._tracker.exists = AsyncMock(  # type: ignore[method-assign]
        return_value={"file_path": str(on_disk)}
    )
    good = FileInfo(file_name="ok.pdf", file_hash="h-ok", file_type="pdf", status=FileStatus.DONE)
    failed = FileInfo(
        file_name="scan.pdf", file_hash="h-scan", file_type="pdf", status=FileStatus.FAILED,
    )
    session = ImportSession(
        session_id="s", status=ImportStatus.READY, files=[good, failed],
        created_at=datetime.now(UTC),
    )
    pipeline._sessions["s"] = session

    processed: list[str] = []

    async def fake_process_one(sess, info, path, kt, proj, equip, start_index, force_ocr=False):
        processed.append(info.file_name)
        info.status = FileStatus.DONE
        return []

    monkeypatch.setattr(pipeline, "_process_one", fake_process_one)
    await pipeline.retry_failed_files("s", force_ocr=True)
    await asyncio.gather(*pipeline._tasks)

    # Only the failed file was retried, not the already-good one.
    assert processed == ["scan.pdf"]
    assert session.status == ImportStatus.READY


# ── A3: recommit_tracking replays a failed tracker write ──────────────────────

async def test_recommit_tracking_recovers_after_commit(stub_pipeline: ip.ImportPipeline):
    """A commit whose tracker write failed leaves a pending payload; once ES is
    healthy, recommit_tracking re-records it and clears the pending set."""
    session = ImportSession(
        session_id="s-rec",
        documents=[_staged(0, "ok-a")],
        files=[FileInfo(
            file_name="f.pptx", file_hash="hash123", file_type="pptx",
            status=FileStatus.DONE,
        )],
        created_at=datetime.now(UTC),
    )
    stub_pipeline._sessions["s-rec"] = session
    failing = AsyncMock(side_effect=RuntimeError("tracker down"))
    stub_pipeline._tracker.record_committed = failing  # type: ignore[method-assign]

    commit = await stub_pipeline.commit_session("s-rec")
    assert commit["tracking_failed"] == 1
    assert "s-rec" in stub_pipeline._pending_tracking

    # ES recovers; replay succeeds and the pending payload is cleared.
    stub_pipeline._tracker.record_committed = AsyncMock()  # type: ignore[method-assign]
    result = await stub_pipeline.recommit_tracking("s-rec")

    assert result["recovered"] == 1
    assert result["still_failed"] == 0
    assert "s-rec" not in stub_pipeline._pending_tracking


async def test_recommit_tracking_noop_when_nothing_pending(stub_pipeline: ip.ImportPipeline):
    stub_pipeline._sessions["s"] = ImportSession(session_id="s")
    result = await stub_pipeline.recommit_tracking("s")
    assert result == {"recovered": 0, "still_failed": 0, "errors": []}


async def test_process_one_force_ocr_overrides_disabled_setting(monkeypatch, tmp_path):
    """force_ocr=True must reach extract_file as ocr_enabled=True even when the
    server config has OCR off."""
    settings = Settings()
    settings.ingest.ocr_enabled = False
    pipeline = ip.ImportPipeline(
        es=SimpleNamespace(), settings=settings,
        embedder=SimpleNamespace(), taxonomy=SimpleNamespace(),
    )
    pipeline._tracker.record_failed = AsyncMock()  # type: ignore[method-assign]

    captured: dict[str, object] = {}

    def fake_extract(path, **kwargs):
        captured.update(kwargs)
        return []  # no pages → short-circuits to FAILED

    monkeypatch.setattr(ip, "extract_file", fake_extract)

    info = FileInfo(
        file_name="scan.pdf", file_hash="h", file_type="pdf", status=FileStatus.PROCESSING,
    )
    session = ImportSession(session_id="s", files=[info])
    docs = await pipeline._process_one(
        session, info, tmp_path / "scan.pdf", None, None, None, 0, force_ocr=True,
    )

    assert captured["ocr_enabled"] is True
    assert docs == []
    assert info.status == FileStatus.FAILED


# ── Collision guard: a staged doc that would overwrite a committed KB doc ──────

def _collide(d: StagedDocument, action: str | None = None) -> StagedDocument:
    d.collision = ExistingDocSnapshot(doc_id=f"id-{d.index}", title="existing")
    d.collision_action = action
    return d


async def test_commit_blocks_unresolved_collision(stub_pipeline: ip.ImportPipeline):
    """An accepted doc whose commit would overwrite an existing KB doc must not
    be indexed until the reviewer resolves the conflict."""
    session = ImportSession(
        session_id="cc1",
        documents=[_collide(_staged(0, "dup"))],  # collision, no action
        created_at=datetime.now(UTC),
    )
    stub_pipeline._sessions["cc1"] = session

    result = await stub_pipeline.commit_session("cc1")

    assert result["committed"] == 0
    assert any("Unresolved conflict" in e.get("error", "") for e in result["errors"])
    assert stub_pipeline._bulk_mock.await_count == 0  # type: ignore[attr-defined]
    assert session.status == ImportStatus.FAILED


async def test_commit_keep_skips_without_overwriting(stub_pipeline: ip.ImportPipeline):
    """'keep' preserves the existing KB doc — the staged doc is skipped, not indexed."""
    session = ImportSession(
        session_id="cc2",
        documents=[_collide(_staged(0, "dup"), "keep"), _staged(1, "fresh")],
        created_at=datetime.now(UTC),
    )
    stub_pipeline._sessions["cc2"] = session

    result = await stub_pipeline.commit_session("cc2")

    assert result["committed"] == 1  # only the fresh doc
    assert result["skipped"] == 1    # the kept collision
    only_call = stub_pipeline._bulk_mock.await_args  # type: ignore[attr-defined]
    assert len(only_call.args[1]) == 1


async def test_commit_overwrite_indexes_the_doc(stub_pipeline: ip.ImportPipeline):
    session = ImportSession(
        session_id="cc3",
        documents=[_collide(_staged(0, "dup"), "overwrite")],
        created_at=datetime.now(UTC),
    )
    stub_pipeline._sessions["cc3"] = session

    result = await stub_pipeline.commit_session("cc3")

    assert result["committed"] == 1
    assert result["errors"] == []


# ── resolve_collision ─────────────────────────────────────────────────────────

def test_resolve_collision_records_action(stub_pipeline: ip.ImportPipeline):
    d = _collide(_staged(0, "x"))
    stub_pipeline._sessions["s"] = ImportSession(session_id="s", documents=[d])
    stub_pipeline.resolve_collision("s", 0, "overwrite", None)
    assert d.collision_action == "overwrite"


def test_resolve_collision_merge_applies_fields(stub_pipeline: ip.ImportPipeline):
    d = _collide(_staged(0, "x"))
    stub_pipeline._sessions["s"] = ImportSession(session_id="s", documents=[d])
    stub_pipeline.resolve_collision("s", 0, "merge", DocumentUpdate(content="merged body"))
    assert d.collision_action == "merge"
    assert d.content == "merged body"


def test_resolve_collision_rejects_unknown_action(stub_pipeline: ip.ImportPipeline):
    d = _collide(_staged(0, "x"))
    stub_pipeline._sessions["s"] = ImportSession(session_id="s", documents=[d])
    with pytest.raises(ValueError, match="Invalid resolution action"):
        stub_pipeline.resolve_collision("s", 0, "bogus", None)


def test_resolve_collision_out_of_range_raises(stub_pipeline: ip.ImportPipeline):
    stub_pipeline._sessions["s"] = ImportSession(session_id="s", documents=[])
    with pytest.raises(IndexError):
        stub_pipeline.resolve_collision("s", 3, "keep", None)


# ── session_summary ───────────────────────────────────────────────────────────

def test_session_summary_counts_every_outcome(stub_pipeline: ip.ImportPipeline):
    new = _staged(0, "new")
    overwrite = _collide(_staged(1, "ow"), "overwrite")
    keep = _collide(_staged(2, "kp"), "keep")
    unresolved = _collide(_staged(3, "ur"))
    rejected = _staged(4, "rej")
    rejected.accepted = False
    variant_a = _staged(5, "v")
    variant_b = _staged(6, "v")
    variant_a.dup_group_id = variant_b.dup_group_id = "g1"
    stub_pipeline._sessions["s"] = ImportSession(
        session_id="s",
        documents=[new, overwrite, keep, unresolved, rejected, variant_a, variant_b],
        files=[FileInfo(
            file_name="dupe.pdf", file_hash="h", file_type="pdf",
            status=FileStatus.SKIPPED_DUPLICATE,
        )],
    )

    s = stub_pipeline.session_summary("s")

    assert s["new"] == 3  # new + the two ungrouped-by-collision variants
    assert s["overwrite"] == 1
    assert s["keep"] == 1
    assert s["unresolved_conflicts"] == 1
    assert s["rejected"] == 1
    assert s["dup_groups"] == 1
    assert s["skipped_duplicate_files"] == 1


def test_build_duplicate_info_summarizes_committed_docs() -> None:
    # A tracker record (as returned by FileTracker.exists) for a file the KB
    # already holds. _build_duplicate_info turns its committed_docs into the
    # summary shown on the duplicate card — without another ES round-trip.
    existing = {
        "file_name": "siemens-alarms.docx",
        "updated_at": "2026-06-05T07:52:52+00:00",
        "import_status": "committed",
        "committed_docs": [
            {"_index": "kb_alarm", "_id": "alarm:1", "_source": {
                "knowledge_type": "alarm", "title": "Drive fault",
                "error_codes": ["300406", "300410"],
            }},
            {"_index": "kb_alarm", "_id": "alarm:2", "_source": {
                "knowledge_type": "alarm", "title": "Bus error",
                "error_codes": ["380001"],
            }},
        ],
    }
    info = ip._build_duplicate_info(existing)
    assert info.doc_count == 2
    assert info.original_file_name == "siemens-alarms.docx"
    assert info.imported_at == "2026-06-05T07:52:52+00:00"
    assert [d.title for d in info.documents] == ["Drive fault", "Bus error"]
    assert info.documents[0].error_codes == ["300406", "300410"]


def test_build_duplicate_info_caps_preview_but_keeps_true_count() -> None:
    # Hundreds of committed docs must not bloat the response: the item list is
    # capped while doc_count still reports the real total for a "+N more" label.
    existing = {
        "file_name": "big.xlsx",
        "updated_at": "2026-06-05T00:00:00+00:00",
        "committed_docs": [
            {"_index": "kb_alarm", "_id": f"alarm:{i}", "_source": {
                "knowledge_type": "alarm", "title": f"A{i}", "error_codes": [str(i)],
            }}
            for i in range(120)
        ],
    }
    info = ip._build_duplicate_info(existing)
    assert info.doc_count == 120
    assert len(info.documents) == ip._DUPLICATE_DOC_PREVIEW_CAP


def test_build_duplicate_info_tolerates_missing_fields() -> None:
    # A sparse / legacy tracker record (no committed_docs, no file_name) must
    # not raise — the duplicate card just shows zero items.
    info = ip._build_duplicate_info({"updated_at": "2026-06-05T00:00:00+00:00"})
    assert info.doc_count == 0
    assert info.documents == []
    assert info.original_file_name is None
