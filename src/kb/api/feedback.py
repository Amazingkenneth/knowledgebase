"""Search relevance feedback — lightweight 👍/👎 capture for ranking insight.

This is observational only: it never alters search results (the zero-fabrication
contract stands). Signals land in the ``kb_search_feedback`` index; the admin
aggregate helps operators decide whether to tune title_boost / vector_weight /
rrf_window.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from kb.api.deps import ESDep
from kb.es.feedback_mappings import FEEDBACK_INDEX_NAME

log = logging.getLogger("kb.api.feedback")

router = APIRouter(prefix="/api/v1", tags=["feedback"])


class FeedbackRequest(BaseModel):
    doc_id: str = Field(min_length=1, max_length=512)
    helpful: bool
    query_text: str | None = Field(default=None, max_length=2000)
    knowledge_type: str | None = None
    project: str | None = None
    equipment: str | None = None
    search_status: str | None = None


class FeedbackResponse(BaseModel):
    status: str = "recorded"


@router.post(
    "/search/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_feedback(
    request: Request, body: FeedbackRequest, es: ESDep,
) -> FeedbackResponse:
    """Record one 👍/👎 on a search result. Best-effort: a storage hiccup must
    not break the user's flow, so failures map to 503 rather than crashing."""
    request_id = getattr(request.state, "request_id", None)
    doc = {
        "doc_id": body.doc_id,
        "helpful": body.helpful,
        "query_text": body.query_text,
        "knowledge_type": body.knowledge_type,
        "project": body.project,
        "equipment": body.equipment,
        "search_status": body.search_status,
        "request_id": request_id,
        "created_at": datetime.now(UTC).isoformat(),
    }
    try:
        await es.index(index=FEEDBACK_INDEX_NAME, document=doc)
    except Exception as exc:
        log.warning("could not record search feedback: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Feedback store unavailable — try again later.",
        ) from exc
    return FeedbackResponse()


class FeedbackSummary(BaseModel):
    total: int
    helpful: int
    unhelpful: int
    helpful_ratio: float | None
    top_unhelpful_queries: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/admin/search-feedback", response_model=FeedbackSummary)
async def feedback_summary(es: ESDep, limit: int = 20) -> FeedbackSummary:
    """Aggregate feedback for ranking tuning: helpful ratio + the queries that
    most often produced an unhelpful result."""
    limit = max(1, min(limit, 100))
    try:
        resp = await es.search(
            index=FEEDBACK_INDEX_NAME,
            body={
                "size": 0,
                "aggs": {
                    "helpful": {"terms": {"field": "helpful"}},
                    "unhelpful_queries": {
                        "filter": {"term": {"helpful": False}},
                        "aggs": {
                            "by_query": {"terms": {"field": "query_text", "size": limit}},
                        },
                    },
                },
            },
        )
    except Exception as exc:
        log.warning("could not aggregate search feedback: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Feedback store unavailable.",
        ) from exc

    aggs = resp.get("aggregations", {})
    helpful = unhelpful = 0
    for bucket in aggs.get("helpful", {}).get("buckets", []):
        # ES renders a boolean terms bucket key as 1/0 with key_as_string "true"/"false".
        if bucket.get("key_as_string") == "true" or bucket.get("key") == 1:
            helpful = int(bucket["doc_count"])
        else:
            unhelpful = int(bucket["doc_count"])
    total = helpful + unhelpful
    top = [
        {"query": b["key"], "unhelpful": int(b["doc_count"])}
        for b in aggs.get("unhelpful_queries", {}).get("by_query", {}).get("buckets", [])
    ]
    return FeedbackSummary(
        total=total,
        helpful=helpful,
        unhelpful=unhelpful,
        helpful_ratio=(helpful / total) if total else None,
        top_unhelpful_queries=top,
    )
