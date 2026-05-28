"""Orchestrates the file import pipeline: hash → extract → segment → stage.

Import sessions are stored in memory (keyed by UUID). If the server restarts
during a preview session, the session is lost and the user must re-import.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from elasticsearch import AsyncElasticsearch
from pydantic import ValidationError
from kb.config import Settings
from kb.es.body_builder import build_body, build_title_text
from kb.es.mappings import alias_name
from kb.models.document import AlarmDoc, ExperienceDoc, KnowledgeDoc, SetupDoc
from kb.models.ingest import (
    FileInfo,
    FileStatus,
    ImportSession,
    ImportStatus,
    StagedDocument,
)
from kb.models.taxonomy import KnowledgeType, Taxonomy
from kb.services.embedding import EmbeddingClient, EmbeddingError
from kb.services.extraction import extract_file
from kb.services.file_tracker import FileTracker, compute_bytes_hash
from kb.services.indexing import IndexingError, _to_es_source, doc_id, validate_against_taxonomy
from kb.services.segmentation import detect_knowledge_type, segment_text

log = logging.getLogger("kb.import_pipeline")


class ImportPipeline:
    def __init__(
        self,
        es: AsyncElasticsearch,
        settings: Settings,
        embedder: EmbeddingClient,
        taxonomy: Taxonomy,
    ):
        self._es = es
        self._settings = settings
        self._embedder = embedder
        self._taxonomy = taxonomy
        self._tracker = FileTracker(es)
        self._sessions: dict[str, ImportSession] = {}

    def refresh_taxonomy(self, taxonomy: Taxonomy) -> None:
        self._taxonomy = taxonomy

    @property
    def tracker(self) -> FileTracker:
        return self._tracker

    def get_session(self, session_id: str) -> ImportSession | None:
        return self._sessions.get(session_id)

    def list_sessions(self, limit: int = 20) -> list[ImportSession]:
        sessions = sorted(
            self._sessions.values(),
            key=lambda s: s.created_at or datetime.min,
            reverse=True,
        )
        return sessions[:limit]

    async def start_upload(
        self,
        files: list[tuple[str, bytes]],  # (filename, content)
        knowledge_type_hint: KnowledgeType | None = None,
        project_hint: str | None = None,
        equipment_hint: str | None = None,
        force: bool = False,
    ) -> ImportSession:
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

            # Persist file to disk
            dest = upload_dir / f"{file_hash}_{filename}"
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

        # Process files asynchronously
        asyncio.create_task(self._process_files(
            session, file_paths, knowledge_type_hint, project_hint, equipment_hint,
        ))
        return session

    async def start_scan(
        self,
        folder_path: str,
        recursive: bool = False,
        knowledge_type_hint: KnowledgeType | None = None,
        project_hint: str | None = None,
        equipment_hint: str | None = None,
        force: bool = False,
    ) -> ImportSession:
        """Scan a server-side folder and start processing."""
        folder = Path(folder_path)
        if not folder.is_dir():
            raise ValueError(f"Folder not found: {folder_path}")

        allowed = set(self._settings.ingest.allowed_extensions)
        pattern = "**/*" if recursive else "*"
        files_to_upload: list[tuple[str, bytes]] = []

        for p in sorted(folder.glob(pattern)):
            if not p.is_file():
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

    async def _process_files(
        self,
        session: ImportSession,
        file_paths: list[tuple[FileInfo, Path | None]],
        knowledge_type_hint: KnowledgeType | None,
        project_hint: str | None,
        equipment_hint: str | None,
    ) -> None:
        """Background processing: extract → segment → stage."""
        all_docs: list[StagedDocument] = []
        doc_index = 0

        for info, path in file_paths:
            if path is None:
                continue

            try:
                # Step 1: extract text
                info.message = "Extracting text…"
                session.message = f"Extracting: {info.file_name}"
                ocr_enabled = self._settings.ingest.ocr_enabled
                pages = extract_file(path, ocr_enabled=ocr_enabled)
                if not pages:
                    info.status = FileStatus.FAILED
                    info.message = "No text extracted"
                    await self._tracker.record_failed(info.file_hash, "No text extracted")
                    continue

                # Step 2: detect knowledge type if not hinted
                kt = knowledge_type_hint
                if kt is None:
                    info.message = f"Detecting document type ({len(pages)} pages)…"
                    session.message = f"Detecting type: {info.file_name}"
                    sample = "\n".join(text for _, text in pages[:3])
                    kt = await detect_knowledge_type(self._settings, sample, pages=pages)

                # Step 3: segment into structured documents
                chunk_chars = self._settings.ingest.segmentation_chunk_chars
                total_chars = sum(len(text) for _, text in pages)
                n_chunks = max(1, -(-total_chars // chunk_chars))  # ceiling div
                info.message = f"Segmenting ({n_chunks} chunk{'s' if n_chunks != 1 else ''})…"
                session.message = f"Segmenting: {info.file_name}"

                def _on_progress(i: int, total: int, _info: FileInfo = info) -> None:
                    _info.message = f"AI analysis: chunk {i}/{total}…"
                    session.message = f"Segmenting {_info.file_name}: {i}/{total}"

                docs = await segment_text(
                    self._settings, pages, kt, info.file_name,
                    project_hint, equipment_hint,
                    on_chunk_progress=_on_progress,
                )

                for doc in docs:
                    doc.index = doc_index
                    doc_index += 1
                all_docs.extend(docs)

                info.status = FileStatus.DONE
                info.message = f"Extracted {len(docs)} documents"

            except ImportError as exc:
                info.status = FileStatus.FAILED
                info.message = str(exc)
                log.error("Missing dependency for %s: %s", info.file_name, exc)
                await self._tracker.record_failed(info.file_hash, str(exc))
            except Exception as exc:
                info.status = FileStatus.FAILED
                info.message = f"Processing failed: {exc}"
                log.error("Failed to process %s: %s", info.file_name, exc, exc_info=True)
                await self._tracker.record_failed(info.file_hash, str(exc))

        session.documents = all_docs
        session.status = ImportStatus.READY
        session.message = ""

    async def commit_session(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        """Commit accepted staged documents to Elasticsearch."""
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")

        accepted = [d for d in session.documents if d.accepted]
        if not accepted:
            return {"committed": 0, "skipped": len(session.documents), "errors": []}

        committed = 0
        skipped = len(session.documents) - len(accepted)
        errors: list[dict[str, Any]] = []
        # Group committed ES actions by source file hash for tracker
        file_committed: dict[str, list[dict[str, Any]]] = {}

        for staged in accepted:
            try:
                doc = _staged_to_knowledge_doc(staged)
                validate_against_taxonomy(doc, self._taxonomy)

                title_vec, body_vec = None, None
                try:
                    vecs = await self._embedder.embed(
                        [build_title_text(doc), build_body(doc)]
                    )
                    title_vec, body_vec = vecs[0], vecs[1]
                except (EmbeddingError, Exception) as exc:
                    log.warning(
                        "Embedding failed for %s: %s — no vectors", doc.title, exc,
                    )

                _id = doc_id(doc)
                source = _to_es_source(doc, title_vec, body_vec)
                index_name = alias_name(self._settings.es.index_prefix, doc.knowledge_type)

                await self._es.index(
                    index=index_name, id=_id,
                    document=source, refresh="wait_for",
                )

                # Track for file_tracker
                file_hash = self._find_file_hash(session, staged.source_file)
                if file_hash:
                    file_committed.setdefault(file_hash, []).append({
                        "_index": index_name,
                        "_id": _id,
                        "_source": source,
                    })

                committed += 1

            except ValidationError as exc:
                missing = [
                    str(e["loc"][0]) for e in exc.errors()
                    if e.get("type") == "string_too_short" and e.get("loc")
                ]
                other = [
                    f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}"
                    for e in exc.errors()
                    if e.get("type") != "string_too_short"
                ]
                parts = (
                    ([f"Missing required fields: {', '.join(missing)}"] if missing else []) + other
                )
                errors.append({
                    "index": staged.index,
                    "title": staged.title or "Untitled",
                    "error": "; ".join(parts) if parts else str(exc),
                })
                break
            except (IndexingError, ValueError) as exc:
                errors.append({"index": staged.index, "title": staged.title or "Untitled", "error": str(exc)})
                break
            except Exception as exc:
                errors.append({"index": staged.index, "title": staged.title or "Untitled", "error": f"Unexpected: {exc}"})
                log.error("Commit failed for doc %d: %s", staged.index, exc, exc_info=True)
                break

        # Update file tracker with committed docs
        for file_hash, docs in file_committed.items():
            try:
                await self._tracker.record_committed(file_hash, docs)
            except Exception as exc:
                log.error("Failed to update tracker for %s: %s", file_hash[:12], exc)

        session.status = ImportStatus.COMMITTED
        return {"committed": committed, "skipped": skipped, "errors": errors}

    def _find_file_hash(self, session: ImportSession, source_file: str) -> str | None:
        for f in session.files:
            if f.file_name == source_file and f.file_hash:
                return f.file_hash
        return None


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
