"""File import endpoints — upload, scan, preview, edit, commit.

POST /api/v1/ingest/upload      Upload files (multipart)
POST /api/v1/ingest/scan        Scan a server-side folder
GET  /api/v1/ingest/sessions     List recent import sessions
GET  /api/v1/ingest/sessions/{id}  Get session status + extracted docs
PUT  /api/v1/ingest/sessions/{id}/documents/{idx}  Edit a staged document
PATCH /api/v1/ingest/sessions/{id}/documents/{idx}  Accept/reject
POST /api/v1/ingest/sessions/{id}/commit  Commit to ES
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile

from kb.models.ingest import (
    AcceptReject,
    CommitResponse,
    DocumentUpdate,
    ImportStatus,
    ScanRequest,
    SessionListItem,
    SessionResponse,
    UploadResponse,
)
from kb.models.taxonomy import KnowledgeType
from kb.services.import_pipeline import ImportPipeline

log = logging.getLogger("kb.api.ingest")

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


def _pipeline(request: Request) -> ImportPipeline:
    return request.app.state.import_pipeline


# ── Upload ───────────────────────────────────────────────────────────────────

@router.post("/upload", response_model=UploadResponse, status_code=202)
async def upload_files(
    request: Request,
    files: list[UploadFile],
    knowledge_type_hint: KnowledgeType | None = Form(default=None),  # noqa: B008
    project_hint: str | None = Form(default=None),  # noqa: B008
    equipment_hint: str | None = Form(default=None),  # noqa: B008
    force: bool = Form(default=False),  # noqa: B008
) -> UploadResponse:
    pipeline = _pipeline(request)

    file_data: list[tuple[str, bytes]] = []
    for f in files:
        content = await f.read()
        file_data.append((f.filename or "unknown", content))

    session = await pipeline.start_upload(
        file_data, knowledge_type_hint, project_hint, equipment_hint, force,
    )
    return UploadResponse(session_id=session.session_id, files=session.files)


# ── Scan ─────────────────────────────────────────────────────────────────────

@router.post("/scan", response_model=UploadResponse, status_code=202)
async def scan_folder(request: Request, body: ScanRequest) -> UploadResponse:
    pipeline = _pipeline(request)
    try:
        session = await pipeline.start_scan(
            body.folder_path, body.recursive,
            body.knowledge_type_hint, body.project_hint, body.equipment_hint,
            body.force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UploadResponse(session_id=session.session_id, files=session.files)


# ── Sessions ─────────────────────────────────────────────────────────────────

@router.get("/sessions", response_model=list[SessionListItem])
async def list_sessions(request: Request, limit: int = 20) -> list[SessionListItem]:
    pipeline = _pipeline(request)
    sessions = pipeline.list_sessions(limit)
    return [
        SessionListItem(
            session_id=s.session_id,
            created_at=s.created_at,
            status=s.status,
            files_count=len(s.files),
            docs_committed=(
                sum(1 for d in s.documents if d.accepted)
                if s.status == ImportStatus.COMMITTED else 0
            ),
        )
        for s in sessions
    ]


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(request: Request, session_id: str) -> SessionResponse:
    pipeline = _pipeline(request)
    session = pipeline.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    processed = sum(1 for f in session.files if f.status.value != "processing")
    return SessionResponse(
        session_id=session.session_id,
        status=session.status,
        message=session.message,
        files_total=len(session.files),
        files_processed=processed,
        files=session.files,
        documents=session.documents,
    )


# ── Document editing ─────────────────────────────────────────────────────────

@router.put("/sessions/{session_id}/documents/{doc_index}")
async def update_document(
    request: Request, session_id: str, doc_index: int, body: DocumentUpdate,
) -> dict[str, str]:
    pipeline = _pipeline(request)
    session = pipeline.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if doc_index < 0 or doc_index >= len(session.documents):
        raise HTTPException(status_code=404, detail="Document index out of range")

    doc = session.documents[doc_index]
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(doc, field, value)

    return {"status": "updated"}


@router.patch("/sessions/{session_id}/documents/{doc_index}")
async def accept_reject_document(
    request: Request, session_id: str, doc_index: int, body: AcceptReject,
) -> dict[str, str]:
    pipeline = _pipeline(request)
    session = pipeline.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if doc_index < 0 or doc_index >= len(session.documents):
        raise HTTPException(status_code=404, detail="Document index out of range")

    session.documents[doc_index].accepted = body.accepted
    return {"status": "updated"}


# ── Commit ───────────────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/commit", response_model=CommitResponse)
async def commit_session(request: Request, session_id: str) -> CommitResponse:
    pipeline = _pipeline(request)
    try:
        result = await pipeline.commit_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CommitResponse(**result)
