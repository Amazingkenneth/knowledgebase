from __future__ import annotations

from fastapi import APIRouter

from kb.api.deps import SearchDep
from kb.models.search import SearchRequest, SearchResponse

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest, svc: SearchDep) -> SearchResponse:
    return await svc.search(req)
