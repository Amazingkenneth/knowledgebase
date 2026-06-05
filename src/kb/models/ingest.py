"""Pydantic models for the file ingestion pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from kb.models.taxonomy import KnowledgeType


class ImportStatus(StrEnum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    READY = "ready_for_review"
    COMMITTED = "committed"
    FAILED = "failed"


class FileStatus(StrEnum):
    PROCESSING = "processing"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    DONE = "done"


class SkippedChunk(BaseModel):
    """A chunk the segmenter declined to extract — surfaced so the user can see
    what was filtered and why, in plain language."""
    source_file: str
    page_range: str
    reason: str  # "non_content" | "no_entries"
    hint: str


class DuplicateDocSummary(BaseModel):
    """One previously-committed document, summarized for the duplicate card."""
    knowledge_type: str = ""
    title: str = ""
    error_codes: list[str] = Field(default_factory=list)


class DuplicateInfo(BaseModel):
    """What the KB already holds for a file the user re-uploaded.

    Surfaced on a SKIPPED_DUPLICATE FileInfo so the UI can explain *why* the
    file was skipped — original name, when it was imported, and the items it
    contributed — instead of a bare "Skipped" badge.
    """
    imported_at: str | None = None
    # The name the file carried when it was first imported. May differ from the
    # just-uploaded name (same content, renamed file).
    original_file_name: str | None = None
    doc_count: int = 0
    # Capped list of item summaries (see _DUPLICATE_DOC_PREVIEW_CAP); doc_count
    # carries the true total so the UI can show "+N more".
    documents: list[DuplicateDocSummary] = Field(default_factory=list)


class FileInfo(BaseModel):
    file_name: str
    file_hash: str
    file_type: str
    file_size: int = 0
    status: FileStatus
    message: str = ""
    # Segmentation progress for a real progress bar in the UI (the message
    # string carries the human label). Both None until segmentation starts.
    chunks_total: int | None = None
    chunks_done: int | None = None
    skipped_chunks: list[SkippedChunk] = Field(default_factory=list)
    # Set only when status == SKIPPED_DUPLICATE: what the KB already holds.
    duplicate_info: DuplicateInfo | None = None


class StagedDocument(BaseModel):
    """A document extracted from a file, pending review before commit."""
    index: int
    knowledge_type: KnowledgeType
    project: str = ""
    equipment: str = ""
    title: str = ""
    error_codes: list[str] = Field(default_factory=list)
    # Type-specific fields (alarm)
    content: str = ""
    resolution: str = ""
    # Type-specific fields (setup)
    procedure: str = ""
    prerequisites: str = ""
    # Type-specific fields (experience)
    body_text: str = ""
    # Common
    notes: str = ""
    source_file: str = ""
    source_pages: list[str] = Field(default_factory=list)
    raw_text_excerpt: str = ""
    confidence: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    accepted: bool = True


class ImportSession(BaseModel):
    session_id: str
    status: ImportStatus = ImportStatus.PENDING
    message: str = ""
    files: list[FileInfo] = Field(default_factory=list)
    documents: list[StagedDocument] = Field(default_factory=list)
    knowledge_type_hint: KnowledgeType | None = None
    project_hint: str | None = None
    equipment_hint: str | None = None
    created_at: datetime | None = None


# ── API request / response shapes ────────────────────────────────────────────

class UploadResponse(BaseModel):
    session_id: str
    files: list[FileInfo]


class ScanRequest(BaseModel):
    folder_path: str
    recursive: bool = False
    knowledge_type_hint: KnowledgeType | None = None
    project_hint: str | None = None
    equipment_hint: str | None = None
    force: bool = False


class SessionResponse(BaseModel):
    session_id: str
    status: ImportStatus
    message: str = ""
    files_total: int
    files_processed: int
    files: list[FileInfo] = Field(default_factory=list)
    documents: list[StagedDocument]


class SessionListItem(BaseModel):
    session_id: str
    created_at: datetime | None
    status: ImportStatus
    files_count: int
    docs_committed: int = 0


class DocumentUpdate(BaseModel):
    """Partial update for a staged document during preview."""
    knowledge_type: KnowledgeType | None = None
    project: str | None = None
    equipment: str | None = None
    title: str | None = None
    error_codes: list[str] | None = None
    content: str | None = None
    resolution: str | None = None
    procedure: str | None = None
    prerequisites: str | None = None
    body_text: str | None = None
    notes: str | None = None
    accepted: bool | None = None


class AcceptReject(BaseModel):
    accepted: bool


class AcceptAllRequest(BaseModel):
    """Accept every staged doc, or only those of a given knowledge type."""
    knowledge_type: KnowledgeType | None = None


class RetryRequest(BaseModel):
    """Options for re-processing a single failed file in a session."""
    # Force OCR on for this retry even when ingest.ocr_enabled is off — lets the
    # reviewer recover an image-only/scanned PDF without a server config change.
    force_ocr: bool = False


class CommitResponse(BaseModel):
    committed: int
    skipped: int
    errors: list[dict[str, Any]] = Field(default_factory=list)
    # Documents indexed without vectors because the embedding service was
    # unavailable at commit time (they remain BM25-searchable).
    vectors_skipped: int = 0
    # Documents indexed into ES but whose import-tracker row failed to update.
    # They are searchable now but would be dropped on the next startup reseed
    # (restore_imports reads the tracker), so the user should re-import them.
    tracking_failed: int = 0


class RecommitTrackingResponse(BaseModel):
    """Result of retrying the tracker writes that failed during commit."""
    # Documents whose tracker row was successfully (re-)recorded this time.
    recovered: int = 0
    # Documents still missing from the tracker after the retry.
    still_failed: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)
