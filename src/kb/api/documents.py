from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field

from kb.api.deps import ESDep, IndexingDep, SettingsDep
from kb.es.mappings import alias_name
from kb.models.document import (
    AlarmDoc,
    ExperienceDoc,
    KnowledgeDoc,
    SetupDoc,
    doc_class_for,
)
from kb.models.taxonomy import KnowledgeType
from kb.services.indexing import IndexingError

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


@router.get("/stats")
async def doc_stats(es: ESDep, settings: SettingsDep) -> dict[str, object]:
    """Doc counts aggregated across all indices — used by the frontend landing page."""
    by_type: dict[str, int] = {}
    by_project: dict[str, int] = {}
    by_equipment: dict[str, int] = {}
    total = 0

    for kt in KnowledgeType:
        index = alias_name(settings.es.index_prefix, kt)
        try:
            agg_resp = await es.search(
                index=index,
                body={
                    "size": 0,
                    "aggs": {
                        "project": {"terms": {"field": "project", "size": 50}},
                        "equipment": {"terms": {"field": "equipment", "size": 50}},
                    },
                },
            )
            count = int(agg_resp["hits"]["total"]["value"])
            by_type[kt.value] = count
            total += count
            for bucket in agg_resp.get("aggregations", {}).get("project", {}).get("buckets", []):
                by_project[bucket["key"]] = by_project.get(bucket["key"], 0) + int(bucket["doc_count"])
            for bucket in agg_resp.get("aggregations", {}).get("equipment", {}).get("buckets", []):
                by_equipment[bucket["key"]] = by_equipment.get(bucket["key"], 0) + int(bucket["doc_count"])
        except Exception:
            by_type[kt.value] = 0

    return {"total": total, "by_type": by_type, "by_project": by_project, "by_equipment": by_equipment}


def _parse_doc(kt: KnowledgeType, payload: dict[str, Any]) -> KnowledgeDoc:
    """Discriminate by URL knowledge_type, then validate against the matching subclass."""
    payload = {**payload, "knowledge_type": kt.value}
    cls = doc_class_for(kt)
    try:
        # Each subclass is annotated to accept only its own KnowledgeType.
        if cls is AlarmDoc:
            return AlarmDoc(**payload)
        if cls is SetupDoc:
            return SetupDoc(**payload)
        return ExperienceDoc(**payload)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


class IndexOneResponse(BaseModel):
    id: str


class BulkResponse(BaseModel):
    indexed: int
    errors: list[dict[str, Any]] = Field(default_factory=list)


@router.post(
    "/{knowledge_type}",
    response_model=IndexOneResponse,
    status_code=status.HTTP_201_CREATED,
)
async def index_one(
    knowledge_type: KnowledgeType,
    payload: dict[str, Any],
    indexing: IndexingDep,
) -> IndexOneResponse:
    doc = _parse_doc(knowledge_type, payload)
    try:
        _id = await indexing.index_one(doc)
    except IndexingError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return IndexOneResponse(id=_id)


@router.post("/{knowledge_type}/_bulk", response_model=BulkResponse)
async def index_bulk(
    knowledge_type: KnowledgeType,
    payloads: list[dict[str, Any]],
    indexing: IndexingDep,
) -> BulkResponse:
    docs: list[KnowledgeDoc] = []
    parse_errors: list[dict[str, Any]] = []
    for i, p in enumerate(payloads):
        try:
            docs.append(_parse_doc(knowledge_type, p))
        except HTTPException as e:
            parse_errors.append({"row": i, "error": e.detail})

    if parse_errors:
        # Don't run a partial bulk; surface all parse errors first.
        return BulkResponse(indexed=0, errors=parse_errors)

    result = await indexing.index_bulk(docs)
    return BulkResponse(**result)


@router.delete(
    "/{knowledge_type}/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_doc(
    knowledge_type: KnowledgeType,
    indexing: IndexingDep,
    doc_id: str = Path(..., min_length=1),
) -> None:
    found = await indexing.delete(knowledge_type.value, doc_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"document {doc_id!r} not found")
