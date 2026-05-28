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


class FileInfo(BaseModel):
    file_name: str
    file_hash: str
    file_type: str
    file_size: int = 0
    status: FileStatus
    message: str = ""


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


class CommitResponse(BaseModel):
    committed: int
    skipped: int
    errors: list[dict[str, Any]] = Field(default_factory=list)
