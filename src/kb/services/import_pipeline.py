"""Orchestrates the file import pipeline: hash → extract → segment → stage.

Import sessions are stored in memory (keyed by UUID). If the server restarts
during a preview session, the session is lost and the user must re-import.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from kb.services.embedding import EmbeddingClient, EmbeddingError
from kb.services.extraction import extract_file
from kb.services.file_tracker import FileTracker, compute_bytes_hash
from kb.services.indexing import IndexingError, _to_es_source, doc_id, validate_against_taxonomy
from kb.services.segmentation import segment_text

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

                # Step 2: segment into structured documents.
                # If knowledge_type_hint is set, every chunk goes through that
                # parser (lock). If None, each chunk is classified independently
                # → supports mixed-type files; non-content pages are skipped.
                chunk_chars = self._settings.ingest.segmentation_chunk_chars
                total_chars = sum(len(text) for _, text in pages)
                n_chunks = max(1, -(-total_chars // chunk_chars))  # ceiling div
                info.message = f"Segmenting ({n_chunks} chunk{'s' if n_chunks != 1 else ''})…"
                session.message = f"Segmenting: {info.file_name}"

                def _on_progress(i: int, total: int, _info: FileInfo = info) -> None:
                    _info.message = f"AI analysis: chunk {i}/{total}…"
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
                )

                # Resolve project/equipment against the taxonomy. Entry values
                # supplied by the LLM (verbatim from the source) take priority,
                # then filename-detected hints (already folded into the doc by
                # the segmenter), then the cross-project bucket "所有项目" so
                # the reviewer is never blocked from committing.
                for doc in docs:
                    _resolve_taxonomy_fields(doc, self._taxonomy, info.file_name)
                    doc.index = doc_index
                    doc_index += 1
                all_docs.extend(docs)
                info.skipped_chunks = skipped

                info.status = FileStatus.DONE
                info.message = _build_extraction_summary(len(docs), skipped, knowledge_type_hint)

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
                errors.append({
                    "index": staged.index,
                    "title": staged.title or "Untitled",
                    "error": _friendly_validation_message(exc),
                    "hint": "Edit this document in the preview and click Save, then commit again.",
                })
                break
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
                break
            except Exception as exc:
                errors.append({
                    "index": staged.index,
                    "title": staged.title or "Untitled",
                    "error": f"Unexpected: {exc}",
                    "hint": "This is a server-side issue. Check server logs for details.",
                })
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
