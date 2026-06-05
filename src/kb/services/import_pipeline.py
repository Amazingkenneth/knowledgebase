"""Orchestrates the file import pipeline: hash → extract → segment → stage.

Import sessions are stored in memory (keyed by UUID). If the server restarts
during a preview session, the session is lost and the user must re-import.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from elasticsearch.helpers import async_bulk
from pydantic import ValidationError

from elasticsearch import AsyncElasticsearch
from kb.config import Settings
from kb.es.body_builder import build_body, build_title_text
from kb.es.mappings import alias_name
from kb.models.document import AlarmDoc, ExperienceDoc, KnowledgeDoc, SetupDoc
from kb.models.ingest import (
    FileInfo,
    FileStatus,
    ImportSession,
    ImportStatus,
    SkippedChunk,
    StagedDocument,
)
from kb.models.taxonomy import KnowledgeType, Taxonomy
from kb.observability import metrics
from kb.services.embedding import EmbeddingClient, EmbeddingError
from kb.services.extraction import ScannedPdfError, extract_file
from kb.services.file_tracker import FileTracker, compute_bytes_hash
from kb.services.indexing import IndexingError, _to_es_source, doc_id, validate_against_taxonomy
from kb.services.llm import LLMClient
from kb.services.segmentation import segment_text

log = logging.getLogger("kb.import_pipeline")


class ImportPipeline:
    def __init__(
        self,
        es: AsyncElasticsearch,
        settings: Settings,
        embedder: EmbeddingClient,
        taxonomy: Taxonomy,
        llm: LLMClient | None = None,
    ):
        self._es = es
        self._settings = settings
        self._embedder = embedder
        self._taxonomy = taxonomy
        # Shared LLM client for segmentation. Optional so existing tests that
        # construct the pipeline without one still work (segment_text builds a
        # short-lived client from settings when none is passed).
        self._llm = llm
        self._tracker = FileTracker(es)
        self._sessions: dict[str, ImportSession] = {}
        # Bounded FIFO memory of session ids that were evicted by TTL. Lets
        # session_state() tell an *expired* preview (return 410 "re-upload")
        # apart from one that never existed (404). Capped so it can't grow
        # without bound; values are unused (an ordered set).
        self._evicted: OrderedDict[str, None] = OrderedDict()
        # Per-session tracker writes that failed during commit_session, keyed by
        # session_id → {file_hash: committed_doc_payloads}. The docs are already
        # in ES; replaying these via recommit_tracking() makes them durable
        # (restore_imports reads the tracker) without a re-upload.
        self._pending_tracking: dict[str, dict[str, list[dict[str, Any]]]] = {}
        # Strong references to in-flight background tasks. asyncio only holds a
        # weak reference to a task, so without this set a processing task can be
        # garbage-collected mid-run, silently aborting an import.
        self._tasks: set[asyncio.Task[None]] = set()

    # Upper bound on remembered evicted-session ids (see ``_evicted``).
    _EVICTED_CAP = 2000

    def refresh_taxonomy(self, taxonomy: Taxonomy) -> None:
        self._taxonomy = taxonomy

    @property
    def tracker(self) -> FileTracker:
        return self._tracker

    def get_session(self, session_id: str) -> ImportSession | None:
        return self._sessions.get(session_id)

    def session_state(self, session_id: str) -> str | None:
        """Classify a *missing* session id: ``"expired"`` if it was evicted by
        TTL, else ``None`` (never existed). Callers use this to return 410 vs
        404. Live sessions should be fetched via ``get_session`` first.
        """
        if session_id in self._sessions:
            return "live"
        if session_id in self._evicted:
            return "expired"
        return None

    def list_sessions(self, limit: int = 20) -> list[ImportSession]:
        sessions = sorted(
            self._sessions.values(),
            key=lambda s: s.created_at or datetime.min,
            reverse=True,
        )
        return sessions[:limit]

    def evict_expired_sessions(self) -> None:
        """Public entry point for the background sweeper (see ``main.lifespan``)."""
        self._evict_expired_sessions()

    def _evict_expired_sessions(self) -> None:
        """Drop expired sessions so the in-memory ``_sessions`` dict stays bounded.

        Two TTLs:
          * soft (``session_ttl_minutes``): evict terminal (COMMITTED/FAILED)
            sessions — their work is done.
          * hard (``session_hard_ttl_minutes``): evict *any* session, including
            EXTRACTING/READY ones, so an abandoned preview can't pin memory
            forever. The hard TTL is longer, giving real reviews time to finish.
        """
        now = datetime.now(UTC)
        soft_cutoff = now - timedelta(minutes=self._settings.ingest.session_ttl_minutes)
        hard_cutoff = now - timedelta(minutes=self._settings.ingest.session_hard_ttl_minutes)
        terminal = {ImportStatus.COMMITTED, ImportStatus.FAILED}
        expired = []
        for sid, s in self._sessions.items():
            created = s.created_at or now
            if s.status in terminal and created < soft_cutoff or created < hard_cutoff:
                expired.append(sid)
        for sid in expired:
            del self._sessions[sid]
            self._remember_evicted(sid)
        if expired:
            log.info("Evicted %d expired import session(s)", len(expired))

    def _remember_evicted(self, session_id: str) -> None:
        """Record an evicted id so ``session_state`` can report 410 (expired),
        trimming oldest entries to keep the set bounded."""
        self._evicted[session_id] = None
        self._evicted.move_to_end(session_id)
        self._pending_tracking.pop(session_id, None)
        while len(self._evicted) > self._EVICTED_CAP:
            self._evicted.popitem(last=False)

    async def start_upload(
        self,
        files: list[tuple[str, bytes]],  # (filename, content)
        knowledge_type_hint: KnowledgeType | None = None,
        project_hint: str | None = None,
        equipment_hint: str | None = None,
        force: bool = False,
    ) -> ImportSession:
        self._evict_expired_sessions()
        session_id = str(uuid.uuid4())
        session = ImportSession(
            session_id=session_id,
            status=ImportStatus.EXTRACTING,
            knowledge_type_hint=knowledge_type_hint,
            project_hint=project_hint,
            equipment_hint=equipment_hint,
            created_at=datetime.now(UTC),
        )
        self._sessions[session_id] = session

        upload_dir = Path(self._settings.ingest.upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        allowed = set(self._settings.ingest.allowed_extensions)
        max_size = self._settings.ingest.max_file_size_mb * 1024 * 1024

        file_paths: list[tuple[FileInfo, Path | None]] = []

        for filename, content in files:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in allowed:
                info = FileInfo(
                    file_name=filename, file_hash="", file_type=ext,
                    status=FileStatus.UNSUPPORTED,
                    message=f"Unsupported file type: {ext}",
                )
                session.files.append(info)
                file_paths.append((info, None))
                continue

            if len(content) > max_size:
                info = FileInfo(
                    file_name=filename, file_hash="", file_type=ext,
                    file_size=len(content), status=FileStatus.FAILED,
                    message=f"File too large: {len(content)} bytes (max {max_size})",
                )
                session.files.append(info)
                file_paths.append((info, None))
                continue

            file_hash = compute_bytes_hash(content)

            if not force:
                existing = await self._tracker.exists(file_hash)
                if existing and existing.get("import_status") == "committed":
                    info = FileInfo(
                        file_name=filename, file_hash=file_hash, file_type=ext,
                        file_size=len(content), status=FileStatus.SKIPPED_DUPLICATE,
                        message=f"Already imported on {existing.get('updated_at', 'unknown')}",
                    )
                    session.files.append(info)
                    file_paths.append((info, None))
                    continue

            # Persist file to disk. The filename is attacker-controlled (raw
            # multipart value), so reduce it to a safe basename and confirm the
            # destination stays inside upload_dir before writing — otherwise a
            # name like "../../etc/cron.d/x" would escape the upload directory.
            dest = _safe_upload_path(upload_dir, file_hash, filename)
            dest.write_bytes(content)

            info = FileInfo(
                file_name=filename, file_hash=file_hash, file_type=ext,
                file_size=len(content), status=FileStatus.PROCESSING,
            )
            session.files.append(info)
            file_paths.append((info, dest))

            await self._tracker.record_pending(
                file_hash=file_hash, file_name=filename,
                file_path=str(dest), file_size=len(content), file_type=ext,
            )

        # Process files asynchronously. Keep a strong reference so the task is
        # not garbage-collected before it finishes, and surface any unhandled
        # failure (one raised outside the per-file try in _process_files) on the
        # session instead of losing it silently.
        task = asyncio.create_task(self._process_files(
            session, file_paths, knowledge_type_hint, project_hint, equipment_hint,
        ))
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._on_task_done(session, t))
        return session

    def _on_task_done(self, session: ImportSession, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "Import processing task crashed for session %s: %s",
                session.session_id, exc, exc_info=exc,
            )
            session.status = ImportStatus.FAILED
            session.message = f"Processing failed: {exc}"

    async def start_scan(
        self,
        folder_path: str,
        recursive: bool = False,
        knowledge_type_hint: KnowledgeType | None = None,
        project_hint: str | None = None,
        equipment_hint: str | None = None,
        force: bool = False,
    ) -> ImportSession:
        """Scan a server-side folder and start processing.

        The folder must resolve to a path inside ``ingest.scan_root`` so callers
        can't read arbitrary host paths. Symlinked entries are skipped to prevent
        escaping the root via links.
        """
        scan_root = Path(self._settings.ingest.scan_root).resolve()
        folder = Path(folder_path).resolve()
        if not folder.is_dir():
            raise ValueError(f"Folder not found: {folder_path}")
        if folder != scan_root and not folder.is_relative_to(scan_root):
            raise ValueError(
                f"Folder must be inside the allowed scan root ({scan_root})"
            )

        allowed = set(self._settings.ingest.allowed_extensions)
        pattern = "**/*" if recursive else "*"
        files_to_upload: list[tuple[str, bytes]] = []

        for p in sorted(folder.glob(pattern)):
            if p.is_symlink() or not p.is_file():
                continue
            ext = p.suffix.lower().lstrip(".")
            if ext not in allowed:
                continue
            content = p.read_bytes()
            files_to_upload.append((p.name, content))

        if not files_to_upload:
            raise ValueError(f"No supported files found in {folder_path}")

        return await self.start_upload(
            files_to_upload, knowledge_type_hint, project_hint, equipment_hint, force,
        )

    def accept_all(self, session_id: str, knowledge_type: KnowledgeType | None = None) -> int:
        """Mark every staged doc as accepted (or only those of ``knowledge_type``).

        Returns the number of docs flipped to accepted. Lets a reviewer approve a
        whole batch — or all alarms in a mixed file — in one click.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        n = 0
        for doc in session.documents:
            if knowledge_type is not None and doc.knowledge_type != knowledge_type:
                continue
            if not doc.accepted:
                n += 1
            doc.accepted = True
        return n

    async def retry_failed_files(
        self,
        session_id: str,
        force_ocr: bool = False,
    ) -> ImportSession:
        """Re-process *all* FAILED files in a session in one go (bulk retry).

        Each file is re-run the same way as ``retry_file``; missing source files
        are left FAILED with a clear message rather than aborting the batch.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        failed = [f for f in session.files if f.status == FileStatus.FAILED]
        if not failed:
            return session
        session.status = ImportStatus.EXTRACTING
        session.message = f"Retrying {len(failed)} failed file(s)…"
        for info in failed:
            rec = await self._tracker.exists(info.file_hash)
            path = Path(rec["file_path"]) if rec and rec.get("file_path") else None
            if path is None or not path.exists():
                info.message = "Uploaded file no longer available — please re-upload it."
                continue
            session.documents = [
                d for d in session.documents if d.source_file != info.file_name
            ]
            info.status = FileStatus.PROCESSING
            info.message = "Retrying…"
            info.skipped_chunks = []
            task = asyncio.create_task(self._retry_one(session, info, path, force_ocr))
            self._tasks.add(task)
            task.add_done_callback(lambda t: self._on_task_done(session, t))
        return session

    async def retry_file(
        self,
        session_id: str,
        file_hash: str,
        force_ocr: bool = False,
    ) -> ImportSession:
        """Re-process a single failed file within an existing session.

        The uploaded bytes were persisted to disk at upload time and the path is
        recorded in the file tracker, so we can re-run extract→segment→stage for
        just this file without the user re-uploading the whole batch. ``force_ocr``
        lets the reviewer retry an image-only PDF with OCR turned on.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        info = next((f for f in session.files if f.file_hash == file_hash), None)
        if info is None:
            raise ValueError(f"File not found in session: {file_hash}")

        rec = await self._tracker.exists(file_hash)
        path = Path(rec["file_path"]) if rec and rec.get("file_path") else None
        if path is None or not path.exists():
            raise ValueError(
                "Uploaded file is no longer available on the server — please re-upload it."
            )

        # Drop any docs previously staged from this file so a retry replaces
        # rather than duplicates them (a failed file has none, but be defensive).
        session.documents = [d for d in session.documents if d.source_file != info.file_name]
        info.status = FileStatus.PROCESSING
        info.message = "Retrying…"
        info.skipped_chunks = []
        session.status = ImportStatus.EXTRACTING
        session.message = f"Retrying: {info.file_name}"

        task = asyncio.create_task(self._retry_one(session, info, path, force_ocr))
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._on_task_done(session, t))
        return session

    async def _retry_one(
        self,
        session: ImportSession,
        info: FileInfo,
        path: Path,
        force_ocr: bool,
    ) -> None:
        """Background task body for ``retry_file``: process one file and merge
        its docs back into the session, then return the session to READY."""
        start_index = max((d.index for d in session.documents), default=-1) + 1
        docs = await self._process_one(
            session, info, path,
            session.knowledge_type_hint, session.project_hint, session.equipment_hint,
            start_index, force_ocr=force_ocr,
        )
        session.documents.extend(docs)
        session.documents.sort(key=lambda d: d.index)
        session.status = ImportStatus.READY
        session.message = ""

    async def _process_files(
        self,
        session: ImportSession,
        file_paths: list[tuple[FileInfo, Path | None]],
        knowledge_type_hint: KnowledgeType | None,
        project_hint: str | None,
        equipment_hint: str | None,
    ) -> None:
        """Background processing: extract → segment → stage each file in turn."""
        all_docs: list[StagedDocument] = []
        doc_index = 0

        for info, path in file_paths:
            if path is None:
                continue
            docs = await self._process_one(
                session, info, path,
                knowledge_type_hint, project_hint, equipment_hint,
                doc_index,
            )
            all_docs.extend(docs)
            doc_index += len(docs)

        session.documents = all_docs
        session.status = ImportStatus.READY
        session.message = ""

    async def _process_one(
        self,
        session: ImportSession,
        info: FileInfo,
        path: Path,
        knowledge_type_hint: KnowledgeType | None,
        project_hint: str | None,
        equipment_hint: str | None,
        start_index: int,
        force_ocr: bool = False,
    ) -> list[StagedDocument]:
        """Extract → segment → stage a single file.

        Updates ``info.status``/``info.message`` and records tracker failures in
        place. Staged docs are assigned ``doc.index`` sequentially from
        ``start_index``. Returns the staged documents ([] when the file failed or
        yielded nothing). ``force_ocr`` turns OCR on even when
        ``ingest.ocr_enabled`` is off — used by the single-file retry path.
        """
        ocr_enabled = force_ocr or self._settings.ingest.ocr_enabled
        try:
            # Step 1: extract text. extract_file is synchronous and CPU-bound
            # (pymupdf / python-docx / openpyxl / PaddleOCR); run it in a worker
            # thread so a large or OCR-heavy file can't freeze the single async
            # event loop and stall every other in-flight request (uploads, status
            # polls, healthchecks) — which a fronting proxy reports as a 408/504.
            info.message = "Extracting text…"
            session.message = f"Extracting: {info.file_name}"
            pages = await asyncio.to_thread(
                extract_file,
                path,
                ocr_enabled=ocr_enabled,
                ocr_lang=self._settings.ingest.ocr_lang,
                ocr_min_confidence=self._settings.ingest.ocr_min_confidence,
                pdf_max_pages=self._settings.ingest.pdf_max_pages,
                xlsx_max_cells=self._settings.ingest.xlsx_max_cells,
            )
            if not pages:
                info.status = FileStatus.FAILED
                info.message = "No text extracted"
                await self._tracker.record_failed(info.file_hash, "No text extracted")
                metrics.record_import_file("failed")
                return []

            # Step 2: segment into structured documents.
            # If knowledge_type_hint is set, every chunk goes through that
            # parser (lock). If None, each chunk is classified independently
            # → supports mixed-type files; non-content pages are skipped.
            chunk_chars = self._settings.ingest.segmentation_chunk_chars
            total_chars = sum(len(text) for _, text in pages)
            n_chunks = max(1, -(-total_chars // chunk_chars))  # ceiling div
            info.message = f"Segmenting ({n_chunks} chunk{'s' if n_chunks != 1 else ''})…"
            info.chunks_total = n_chunks
            info.chunks_done = 0
            session.message = f"Segmenting: {info.file_name}"

            def _on_progress(i: int, total: int, _info: FileInfo = info) -> None:
                _info.message = f"AI analysis: chunk {i}/{total}…"
                _info.chunks_total = total
                _info.chunks_done = i
                session.message = f"Segmenting {_info.file_name}: {i}/{total}"

            # If no knowledge_type_hint, pass None → per-chunk routing
            # (supports mixed-type files and skips non-content pages).
            seg_type = knowledge_type_hint

            # Filename-based hint fallback. If the user didn't supply
            # project/equipment hints but the filename contains a token
            # that matches a taxonomy value (e.g. "PDX-aligner-faults.pdf"
            # → project=PDX, equipment=Aligner), use that as the hint.
            # User can still override per-doc in the preview UI.
            effective_project = project_hint
            effective_equipment = equipment_hint
            if not effective_project or not effective_equipment:
                fn_project, fn_equipment = _detect_taxonomy_from_filename(
                    info.file_name, self._taxonomy,
                )
                if not effective_project and fn_project:
                    effective_project = fn_project
                    log.info(
                        "Auto-detected project=%s from filename %s",
                        fn_project, info.file_name,
                    )
                if not effective_equipment and fn_equipment:
                    effective_equipment = fn_equipment
                    log.info(
                        "Auto-detected equipment=%s from filename %s",
                        fn_equipment, info.file_name,
                    )

            docs, skipped = await segment_text(
                self._settings, pages, seg_type, info.file_name,
                effective_project, effective_equipment,
                on_chunk_progress=_on_progress,
                llm=self._llm,
            )

            # Resolve project/equipment against the taxonomy. Entry values
            # supplied by the LLM (verbatim from the source) take priority,
            # then filename-detected hints (already folded into the doc by
            # the segmenter), then the cross-project bucket "所有项目" so
            # the reviewer is never blocked from committing.
            idx = start_index
            for doc in docs:
                _resolve_taxonomy_fields(doc, self._taxonomy, info.file_name)
                doc.index = idx
                idx += 1
            info.skipped_chunks = skipped

            info.status = FileStatus.DONE
            info.chunks_done = info.chunks_total
            info.message = _build_extraction_summary(len(docs), skipped, knowledge_type_hint)
            metrics.record_import_file("done")
            metrics.record_import_docs("extracted", len(docs))
            return docs

        except ScannedPdfError as exc:
            # Image-only PDF with no readable text — point the user at OCR
            # instead of a generic failure.
            from kb.services.ocr import ocr_available

            if not ocr_enabled:
                hint = "Enable OCR (KB_INGEST__OCR_ENABLED=true) and re-import."
            elif not ocr_available():
                hint = (
                    "OCR is not installed in this deployment — rebuild the image "
                    "with --build-arg INSTALL_OCR=true, then re-import."
                )
            else:
                hint = "OCR ran but found no readable text (low-quality scan)."
            msg = f"{exc} {hint}"
            info.status = FileStatus.FAILED
            info.message = msg
            log.warning("Scanned PDF %s: %s", info.file_name, msg)
            await self._tracker.record_failed(info.file_hash, msg)
            metrics.record_import_file("failed")
            return []
        except ImportError as exc:
            info.status = FileStatus.FAILED
            info.message = str(exc)
            log.error("Missing dependency for %s: %s", info.file_name, exc)
            await self._tracker.record_failed(info.file_hash, str(exc))
            metrics.record_import_file("failed")
            return []
        except Exception as exc:
            info.status = FileStatus.FAILED
            info.message = f"Processing failed: {exc}"
            log.error("Failed to process %s: %s", info.file_name, exc, exc_info=True)
            await self._tracker.record_failed(info.file_hash, str(exc))
            metrics.record_import_file("failed")
            return []

    async def commit_session(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        """Commit accepted staged documents to Elasticsearch."""
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")

        commit_start = time.perf_counter()
        accepted = [d for d in session.documents if d.accepted]
        skipped = len(session.documents) - len(accepted)
        if not accepted:
            return {"committed": 0, "skipped": skipped, "errors": [], "vectors_skipped": 0}

        errors: list[dict[str, Any]] = []

        # Phase 1 — convert + validate every accepted doc before touching ES, so
        # a bad doc surfaces a clear error rather than a half-committed batch.
        prepared: list[tuple[StagedDocument, KnowledgeDoc]] = []
        for staged in accepted:
            try:
                doc = _staged_to_knowledge_doc(staged)
                validate_against_taxonomy(doc, self._taxonomy)
                prepared.append((staged, doc))
            except ValidationError as exc:
                errors.append({
                    "index": staged.index,
                    "title": staged.title or "Untitled",
                    "error": _friendly_validation_message(exc),
                    "hint": "Edit this document in the preview and click Save, then commit again.",
                })
            except (IndexingError, ValueError) as exc:
                msg = str(exc)
                hint = (
                    "Check that the project/equipment values match config/taxonomy.yaml."
                    if "taxonomy" in msg.lower() or "not in" in msg.lower()
                    else "Fix the document in the preview and try again."
                )
                errors.append({
                    "index": staged.index,
                    "title": staged.title or "Untitled",
                    "error": msg,
                    "hint": hint,
                })
            except Exception as exc:
                errors.append({
                    "index": staged.index,
                    "title": staged.title or "Untitled",
                    "error": f"Unexpected: {exc}",
                    "hint": "This is a server-side issue. Check server logs for details.",
                })
                log.error("Commit failed for doc %d: %s", staged.index, exc, exc_info=True)

        if not prepared:
            session.status = ImportStatus.FAILED
            return {"committed": 0, "skipped": skipped, "errors": errors, "vectors_skipped": 0}

        # Phase 2 — embed all docs in one batched call. Vectors are best-effort:
        # if the embedding service is down we still index (BM25-only) and report
        # how many docs went in without vectors.
        vectors_skipped = 0
        texts: list[str] = []
        for _, doc in prepared:
            texts.append(build_title_text(doc))
            texts.append(build_body(doc))
        vectors: list[list[float] | None]
        try:
            embedded = await self._embedder.embed(texts)
            vectors = list(embedded)
        except (EmbeddingError, OSError, RuntimeError) as exc:
            log.warning("Batch embedding failed during commit: %s — indexing without vectors", exc)
            vectors = [None] * len(texts)
            vectors_skipped = len(prepared)

        # Phase 3 — bulk index in one request (single refresh) and map any
        # per-doc rejections back to a friendly error.
        actions: list[dict[str, Any]] = []
        meta: list[tuple[StagedDocument, str, str, dict[str, Any]]] = []
        for i, (staged, doc) in enumerate(prepared):
            _id = doc_id(doc)
            source = _to_es_source(doc, vectors[2 * i], vectors[2 * i + 1])
            index_name = alias_name(self._settings.es.index_prefix, doc.knowledge_type)
            actions.append({"_index": index_name, "_id": _id, "_source": source})
            meta.append((staged, _id, index_name, source))

        try:
            success, bulk_errors = await async_bulk(
                self._es, actions, raise_on_error=False, refresh="wait_for"
            )
        except Exception as exc:
            log.error("Bulk index failed during commit: %s", exc, exc_info=True)
            session.status = ImportStatus.FAILED
            errors.append({
                "error": f"Bulk index failed: {exc}",
                "hint": "This is a server-side issue. Check server logs / Elasticsearch.",
            })
            return {"committed": 0, "skipped": skipped, "errors": errors,
                    "vectors_skipped": vectors_skipped}

        # async_bulk returns the error list when stats_only is False (the default);
        # the int form only appears with stats_only=True, so guard for the type.
        failed_ids: set[str] = set()
        for be in bulk_errors if isinstance(bulk_errors, list) else []:
            op = next(iter(be.values())) if isinstance(be, dict) else {}
            fid = op.get("_id", "")
            failed_ids.add(fid)
            errors.append({
                "error": str(op.get("error") or be),
                "hint": "Elasticsearch rejected this document. Check server logs.",
            })

        committed = int(success)

        # Track committed docs per source file for restore_imports().
        file_committed: dict[str, list[dict[str, Any]]] = {}
        for staged, _id, index_name, source in meta:
            if _id in failed_ids:
                continue
            file_hash = self._find_file_hash(session, staged.source_file)
            if file_hash:
                file_committed.setdefault(file_hash, []).append({
                    "_index": index_name, "_id": _id, "_source": source,
                })
        tracking_failed = 0
        for file_hash, docs in file_committed.items():
            try:
                await self._tracker.record_committed(file_hash, docs)
            except Exception as exc:
                # The docs ARE in ES, but the tracker row that drives
                # restore_imports() didn't update — so the next startup reseed
                # would silently drop them. Stash the payload for replay via
                # recommit_tracking() and surface it instead of only logging.
                tracking_failed += len(docs)
                self._pending_tracking.setdefault(session_id, {})[file_hash] = docs
                log.error("Failed to update tracker for %s: %s", file_hash[:12], exc)
                errors.append({
                    "error": (
                        f"Indexed {len(docs)} doc(s) but failed to record them "
                        f"for restore: {exc}"
                    ),
                    "hint": (
                        "Documents are searchable now but won't survive a server "
                        "restart/reseed. Use \"Make durable\" to retry recording them."
                    ),
                })

        # A clean or partial run is COMMITTED (errors list carries what dropped);
        # only a run where nothing landed is FAILED.
        session.status = ImportStatus.FAILED if committed == 0 else ImportStatus.COMMITTED
        metrics.IMPORT_COMMIT_DURATION.observe(time.perf_counter() - commit_start)
        metrics.record_import_docs("committed", committed)
        metrics.record_import_docs("rejected", len(accepted) - committed)
        return {"committed": committed, "skipped": skipped, "errors": errors,
                "vectors_skipped": vectors_skipped, "tracking_failed": tracking_failed}

    async def recommit_tracking(self, session_id: str) -> dict[str, Any]:
        """Retry the tracker writes that failed during commit (durability recovery).

        The docs are already in ES; this only re-records them in
        ``kb_import_files`` so ``restore_imports()`` keeps them across a reseed.
        Idempotent: successfully recorded files are dropped from the pending set.
        """
        pending = self._pending_tracking.get(session_id, {})
        if not pending:
            return {"recovered": 0, "still_failed": 0, "errors": []}

        recovered = 0
        errors: list[dict[str, Any]] = []
        for file_hash in list(pending.keys()):
            docs = pending[file_hash]
            try:
                await self._tracker.record_committed(file_hash, docs)
                recovered += len(docs)
                del pending[file_hash]
            except Exception as exc:
                log.error("recommit_tracking failed for %s: %s", file_hash[:12], exc)
                errors.append({
                    "error": f"Still could not record {len(docs)} doc(s): {exc}",
                    "hint": "Elasticsearch may still be unavailable — try again shortly.",
                })
        if not pending:
            self._pending_tracking.pop(session_id, None)
        still_failed = sum(len(d) for d in pending.values())
        return {"recovered": recovered, "still_failed": still_failed, "errors": errors}

    def _find_file_hash(self, session: ImportSession, source_file: str) -> str | None:
        for f in session.files:
            if f.file_name == source_file and f.file_hash:
                return f.file_hash
        return None


def _safe_upload_path(upload_dir: Path, file_hash: str, filename: str) -> Path:
    """Build a write destination under upload_dir that a hostile filename can't escape.

    The filename arrives verbatim from the multipart upload, so it may contain
    directory separators or `..` segments. We collapse it to its basename
    (dropping any POSIX or Windows path components), fall back to "upload" if
    nothing usable remains, and assert the final path is contained in
    upload_dir. The hash prefix keeps distinct uploads from colliding.
    """
    base = PurePosixPath(filename.replace("\\", "/")).name or "upload"
    base = base.lstrip(".") or "upload"  # avoid hidden/".."-style names
    dest = (upload_dir / f"{file_hash}_{base}").resolve()
    root = upload_dir.resolve()
    if not dest.is_relative_to(root):
        raise ValueError(f"Unsafe upload filename: {filename!r}")
    return dest


# Tokenizer used for filename → taxonomy detection. Split on anything that's
# not a CJK character or alphanumeric. Empty tokens are discarded.
_FILENAME_TOKEN_RE = re.compile(r"[^a-zA-Z0-9一-鿿]+")


def _detect_taxonomy_from_filename(
    filename: str, taxonomy: Taxonomy,
) -> tuple[str | None, str | None]:
    """Return (project, equipment) inferred from filename tokens.

    A taxonomy value matches when it appears as a whole lowercase token in
    the filename stem. Substring-of-token matches are rejected to avoid
    false positives like "Stage" matching a filename containing "stages".
    Returns the first match for each axis; None when nothing matches.
    """
    stem = Path(filename).stem
    tokens = {t for t in _FILENAME_TOKEN_RE.split(stem.lower()) if t}
    if not tokens:
        return None, None
    project = next(
        (p for p in taxonomy.projects if p.lower() in tokens),
        None,
    )
    equipment = next(
        (e for e in taxonomy.equipment if e.lower() in tokens),
        None,
    )
    return project, equipment


_DEFAULT_PROJECT_FALLBACK = "所有项目"


def _resolve_taxonomy_fields(
    doc: StagedDocument, taxonomy: Taxonomy, file_name: str,
) -> None:
    """Validate doc.project / doc.equipment against the taxonomy in-place.

    Values are matched case-insensitively but stored as the canonical
    taxonomy casing. Unknown values are cleared (with a reviewer-visible
    warning) so the dropdown doesn't end up holding free-form text the
    commit step would reject anyway. Project finally falls back to the
    `所有项目` cross-project bucket — never bar the user from committing
    a doc just because the source didn't name a known project.
    """
    project_map = {p.lower(): p for p in taxonomy.projects}
    equipment_map = {e.lower(): e for e in taxonomy.equipment}

    raw_project = (doc.project or "").strip()
    raw_equipment = (doc.equipment or "").strip()

    if raw_project:
        canonical = project_map.get(raw_project.lower())
        if canonical:
            doc.project = canonical
        else:
            log.info(
                "Dropping unknown project %r on %s doc %d (not in taxonomy)",
                raw_project, file_name, doc.index,
            )
            doc.warnings.append(f"unknown_project: {raw_project}")
            doc.project = ""
    if raw_equipment:
        canonical = equipment_map.get(raw_equipment.lower())
        if canonical:
            doc.equipment = canonical
        else:
            log.info(
                "Dropping unknown equipment %r on %s doc %d (not in taxonomy)",
                raw_equipment, file_name, doc.index,
            )
            doc.warnings.append(f"unknown_equipment: {raw_equipment}")
            doc.equipment = ""

    if not doc.project and _DEFAULT_PROJECT_FALLBACK in taxonomy.projects:
        doc.project = _DEFAULT_PROJECT_FALLBACK


def _build_extraction_summary(
    n_docs: int,
    skipped: list[SkippedChunk],
    knowledge_type_hint: KnowledgeType | None,
) -> str:
    """Plain-language summary of what happened, shown on the FileInfo card."""
    parts: list[str] = [f"Extracted {n_docs} document{'s' if n_docs != 1 else ''}"]
    if not skipped:
        return parts[0] + "."
    by_reason: dict[str, int] = {}
    for s in skipped:
        by_reason[s.reason] = by_reason.get(s.reason, 0) + 1
    pretty = {
        "non_content": "non-content page(s) skipped (covers/TOC/preface)",
        "no_entries": "page(s) with no extractable entries",
        "parse_failed": "page(s) the AI couldn't parse — review manually",
    }
    summary_bits = [f"{count} {pretty.get(reason, reason)}" for reason, count in by_reason.items()]
    parts.append("; ".join(summary_bits))
    if knowledge_type_hint is None and any(s.reason == "non_content" for s in skipped):
        parts.append(
            "Tip: set a knowledge-type hint on re-upload to force every page through one parser."
        )
    return ". ".join(parts) + "."


_FRIENDLY_FIELD_HINTS = {
    "title": "Add a short title (≤200 chars) that names the entry.",
    "content": "Required for alarms — paste the Definitions / Reaction section.",
    "resolution": "Required for alarms — paste the Remedy / 解除流程 section.",
    "procedure": "Required for setup — paste the numbered steps.",
    "body_text": "Required for experience — paste the failure description.",
    "error_codes": "Provide at least one error code (e.g. 1030, F7011).",
    "project": "Pick a project from the dropdown (must match taxonomy.yaml).",
    "equipment": "Pick equipment from the dropdown (must match taxonomy.yaml).",
}


def _friendly_validation_message(exc: ValidationError) -> str:
    """Convert pydantic ValidationError into a hint the reviewer can act on."""
    bits: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        field = loc.split(".")[0] if loc else ""
        msg = err.get("msg", "invalid")
        hint = _FRIENDLY_FIELD_HINTS.get(field)
        if err.get("type") == "string_too_short":
            bits.append(f"'{field}' is empty. {hint or 'Please fill it in before saving.'}")
        elif hint:
            bits.append(f"'{field}': {msg}. {hint}")
        else:
            bits.append(f"'{loc or field}': {msg}")
    return " ".join(bits) if bits else str(exc)


def _staged_to_knowledge_doc(staged: StagedDocument) -> KnowledgeDoc:
    """Convert a StagedDocument back to a validated KnowledgeDoc."""
    common = {
        "project": staged.project,
        "equipment": staged.equipment,
        "title": staged.title or "Untitled",
        "error_codes": staged.error_codes,
        "source_file": staged.source_file,
        "source_pages": staged.source_pages,
    }

    if staged.knowledge_type == KnowledgeType.ALARM:
        return AlarmDoc(
            **common,
            content=staged.content or "—",
            resolution=staged.resolution or "—",
            notes=staged.notes,
        )
    if staged.knowledge_type == KnowledgeType.SETUP:
        title = staged.title
        if not title and staged.equipment:
            title = f"{staged.equipment} 调试"
        common["title"] = title or "Untitled"
        return SetupDoc(
            **common,
            procedure=staged.procedure or "—",
            prerequisites=staged.prerequisites,
            notes=staged.notes,
        )
    return ExperienceDoc(
        **common,
        body_text=staged.body_text or "—",
        procedure=staged.procedure,
        notes=staged.notes,
    )
