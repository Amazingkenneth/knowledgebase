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
    AcceptAllRequest,
    AcceptReject,
    CommitResponse,
    CommitSummary,
    DocumentUpdate,
    ImportSession,
    ImportStatus,
    RecommitTrackingResponse,
    ResolveCollision,
    RetryRequest,
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
    pipeline: ImportPipeline = request.app.state.import_pipeline
    return pipeline


def _require_session(pipeline: ImportPipeline, session_id: str) -> ImportSession:
    """Return the live session or raise 410 (expired) / 404 (never existed).

    Distinguishing the two lets the frontend prompt a re-import on an expired
    preview instead of showing a dead-end "not found".
    """
    session = pipeline.get_session(session_id)
    if session is not None:
        return session
    if pipeline.session_state(session_id) == "expired":
        raise HTTPException(
            status_code=410,
            detail="Import session expired — please re-upload the file(s).",
        )
    raise HTTPException(status_code=404, detail="Session not found")


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
    session = _require_session(pipeline, session_id)
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


@router.get("/sessions/{session_id}/summary", response_model=CommitSummary)
async def session_summary(request: Request, session_id: str) -> CommitSummary:
    """Pre-commit consequence preview: how many docs are new vs overwrite vs
    kept, plus unresolved conflicts and missing fields. Drives the review banner
    and the commit gate."""
    pipeline = _pipeline(request)
    _require_session(pipeline, session_id)
    try:
        return CommitSummary(**pipeline.session_summary(session_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ── Document editing ─────────────────────────────────────────────────────────

@router.put("/sessions/{session_id}/documents/{doc_index}")
async def update_document(
    request: Request, session_id: str, doc_index: int, body: DocumentUpdate,
) -> dict[str, str]:
    pipeline = _pipeline(request)
    session = _require_session(pipeline, session_id)
    if doc_index < 0 or doc_index >= len(session.documents):
        raise HTTPException(status_code=400, detail="Document index out of range")

    doc = session.documents[doc_index]
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(doc, field, value)

    return {"status": "updated"}


@router.patch("/sessions/{session_id}/documents/{doc_index}/resolve")
async def resolve_collision(
    request: Request, session_id: str, doc_index: int, body: ResolveCollision,
) -> dict[str, str]:
    """Resolve a staged doc that collides with a committed KB doc.

    ``keep`` preserves the existing doc (this one is skipped on commit),
    ``overwrite`` replaces it as-is, ``merge`` replaces it with the merged
    field values. Unblocks the doc for commit.
    """
    pipeline = _pipeline(request)
    _require_session(pipeline, session_id)
    try:
        pipeline.resolve_collision(
            session_id, doc_index, body.action, body.merged_fields,
        )
    except IndexError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "resolved", "action": body.action}


@router.patch("/sessions/{session_id}/documents/{doc_index}")
async def accept_reject_document(
    request: Request, session_id: str, doc_index: int, body: AcceptReject,
) -> dict[str, str]:
    pipeline = _pipeline(request)
    session = _require_session(pipeline, session_id)
    if doc_index < 0 or doc_index >= len(session.documents):
        raise HTTPException(status_code=400, detail="Document index out of range")

    session.documents[doc_index].accepted = body.accepted
    return {"status": "updated"}


# ── Bulk document accept ─────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/documents/accept-all")
async def accept_all_documents(
    request: Request, session_id: str, body: AcceptAllRequest | None = None,
) -> dict[str, int]:
    """Accept every staged doc (or only those of a given knowledge_type)."""
    pipeline = _pipeline(request)
    _require_session(pipeline, session_id)
    try:
        n = pipeline.accept_all(
            session_id, body.knowledge_type if body else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"accepted": n}


# ── Retry a single failed file ───────────────────────────────────────────────

@router.post(
    "/sessions/{session_id}/files/{file_hash}/retry",
    response_model=UploadResponse,
    status_code=202,
)
async def retry_file(
    request: Request,
    session_id: str,
    file_hash: str,
    body: RetryRequest | None = None,
) -> UploadResponse:
    """Re-process one failed file in the session (optionally forcing OCR) so the
    reviewer doesn't have to restart the whole import."""
    pipeline = _pipeline(request)
    try:
        session = await pipeline.retry_file(
            session_id, file_hash, force_ocr=body.force_ocr if body else False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return UploadResponse(session_id=session.session_id, files=session.files)


# ── Retry all failed files ───────────────────────────────────────────────────

@router.post(
    "/sessions/{session_id}/retry-failed",
    response_model=UploadResponse,
    status_code=202,
)
async def retry_failed_files(
    request: Request,
    session_id: str,
    body: RetryRequest | None = None,
) -> UploadResponse:
    """Re-process every FAILED file in the session at once (optionally forcing OCR)."""
    pipeline = _pipeline(request)
    _require_session(pipeline, session_id)
    try:
        session = await pipeline.retry_failed_files(
            session_id, force_ocr=body.force_ocr if body else False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return UploadResponse(session_id=session.session_id, files=session.files)


# ── Commit ───────────────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/commit", response_model=CommitResponse)
async def commit_session(request: Request, session_id: str) -> CommitResponse:
    pipeline = _pipeline(request)
    try:
        result = await pipeline.commit_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CommitResponse(**result)


# ── Durability recovery ──────────────────────────────────────────────────────

@router.post(
    "/sessions/{session_id}/recommit-tracking",
    response_model=RecommitTrackingResponse,
)
async def recommit_tracking(request: Request, session_id: str) -> RecommitTrackingResponse:
    """Retry the tracker writes that failed during commit.

    The documents are already searchable in ES; this re-records them so they
    survive the next startup reseed. Lets the user recover from a transient ES
    blip at commit time without re-uploading. Idempotent.
    """
    pipeline = _pipeline(request)
    _require_session(pipeline, session_id)
    try:
        result = await pipeline.recommit_tracking(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RecommitTrackingResponse(**result)
