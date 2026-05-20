"""Search service — the strict → loose → vector_only pipeline.

This is the heart of the system. Read this with the plan's "Retrieval pipeline"
diagram open.

Decisions encoded here:
  - Filtering is exact-match on keyword fields (Part 1). Filters never affect
    relevance, only inclusion.
  - Keyword query runs against `body` (which already includes Part-1 metadata
    via body_builder) with `title^N` boost where N comes from config.
  - Vector kNN runs on `body_vec` (the long-form representation). title_vec is
    kept for future fan-out but not used by default — keeps the kNN call cheap
    and ranks by content overlap.
  - Hybrid ranking uses RRF when both BM25 and kNN are in play (ES 8.x retriever
    syntax). RRF is rank-based so no score calibration is needed.
"""

from __future__ import annotations

from typing import Any

from elasticsearch import AsyncElasticsearch

from kb.config import Settings
from kb.es.mappings import alias_name, all_alias_pattern
from kb.models.search import (
    DocHit,
    EffectiveParams,
    SearchRequest,
    SearchResponse,
    SearchStatus,
)
from kb.models.taxonomy import KnowledgeType
import httpx

from kb.services.embedding import EmbeddingClient, EmbeddingError

LOOSE_BANNER = "没有完全匹配的知识，以下为相关参考，仅供参考。"
VECTOR_BANNER = "没有关键词匹配的知识，以下基于语义相似度的相关参考，仅供参考。"
NO_HIT_BANNER = "没有找到匹配的知识。请补充关键词或调整筛选条件后重试。"


def _filters(req: SearchRequest) -> list[dict[str, Any]]:
    """Part-1 filter clauses (term/terms). Project/equipment/error_codes."""
    out: list[dict[str, Any]] = []
    if req.project:
        out.append({"term": {"project": req.project}})
    if req.equipment:
        out.append({"term": {"equipment": req.equipment}})
    if req.error_codes:
        # error_codes is multi-valued on the doc. A query providing N codes means
        # the doc must contain at least one of them. Use `terms`.
        out.append({"terms": {"error_codes": req.error_codes}})
    return out


def _kw_multi_match(keywords: list[str], operator: str, title_boost: float) -> dict[str, Any]:
    """Single-clause keyword query for use in strict (AND) and loose (OR) modes."""
    return {
        "multi_match": {
            "query": " ".join(keywords),
            "fields": [f"title^{title_boost}", "body"],
            "operator": operator,
            "type": "best_fields",
        }
    }


def _knn_clause(query_vec: list[float], k: int) -> dict[str, Any]:
    return {
        "field": "body_vec",
        "query_vector": query_vec,
        "k": k,
        "num_candidates": max(k * 4, 100),
    }


def _bm25_query(
    req: SearchRequest, kw_operator: str, title_boost: float,
    query_text: str | None = None,
) -> dict[str, Any]:
    """Build the `query` clause for the standard /_search body.

    - `filter` for Part-1 (exact, no scoring)
    - `must` for keyword multi_match in the requested operator
    - If no keywords, only filters apply (match_all under filter).
    - `query_text` adds a `should` match for the raw sentence when kNN is absent.
    """
    bool_body: dict[str, Any] = {"filter": _filters(req)}
    if req.keywords:
        bool_body["must"] = [_kw_multi_match(req.keywords, kw_operator, title_boost)]
    else:
        bool_body["must"] = [{"match_all": {}}]
    # Boost documents matching the raw sentence (helps when kNN/embedding is down)
    if query_text:
        bool_body["should"] = [
            {"multi_match": {
                "query": query_text,
                "fields": [f"title^{title_boost}", "body"],
                "type": "best_fields",
                "boost": 0.5,
            }}
        ]
    return {"bool": bool_body}


def _hit_to_doc(h: dict[str, Any]) -> DocHit:
    src = h["_source"]
    return DocHit(
        id=h["_id"],
        score=float(h.get("_score") or 0.0),
        knowledge_type=KnowledgeType(src["knowledge_type"]),
        project=src["project"],
        equipment=src["equipment"],
        error_codes=src.get("error_codes", []),
        title=src["title"],
        source_file=src.get("source_file"),
        source_pages=src.get("source_pages", []),
        sections=src.get("sections", {}),
    )


def _index_for(req: SearchRequest, prefix: str) -> str:
    """Return the ES index or comma-joined aliases to search."""
    if req.knowledge_type is None:
        return ",".join(alias_name(prefix, kt) for kt in KnowledgeType)
    return alias_name(prefix, req.knowledge_type)


def _effective(req: SearchRequest) -> EffectiveParams:
    return EffectiveParams(
        knowledge_type=req.knowledge_type,
        project=req.project,
        equipment=req.equipment,
        error_codes=list(req.error_codes),
        keywords=list(req.keywords),
    )


class SearchService:
    def __init__(self, es: AsyncElasticsearch, settings: Settings, embedder: EmbeddingClient):
        self._es = es
        self._settings = settings
        self._embedder = embedder

    # ---------- public ----------

    async def search(self, req: SearchRequest) -> SearchResponse:
        match req.mode:
            case "strict":
                return await self._strict(req)
            case "loose":
                return await self._loose(req)
            case "vector_only":
                return await self._vector_only(req)
            case "auto":
                return await self._auto(req)

    # ---------- state machine ----------

    async def _auto(self, req: SearchRequest) -> SearchResponse:
        strict_resp = await self._strict(req)
        if strict_resp.status in (SearchStatus.STRICT_HIT, SearchStatus.TOO_MANY):
            return strict_resp

        loose_resp = await self._loose(req)
        if loose_resp.status == SearchStatus.LOOSE_HIT:
            return loose_resp

        if req.query_text:
            vec_resp = await self._vector_only(req)
            if vec_resp.status == SearchStatus.VECTOR_ONLY:
                return vec_resp

        return SearchResponse(
            status=SearchStatus.NO_HIT,
            total=0,
            hits=[],
            effective_params=_effective(req),
            banner=NO_HIT_BANNER,
        )

    # ---------- strict ----------

    async def _strict(self, req: SearchRequest) -> SearchResponse:
        cfg = self._settings.search
        index = _index_for(req, self._settings.es.index_prefix)
        knn_ok = False
        if req.query_text:
            try:
                qvec = (await self._embedder.embed([req.query_text]))[0]
                knn_ok = True
            except (EmbeddingError, httpx.HTTPError):
                qvec = None
        else:
            qvec = None
        body: dict[str, Any] = {
            "size": max(req.size, cfg.strict_max_hits + 1),
            "from": req.from_,
            "query": _bm25_query(
                req, "and", cfg.title_boost,
                query_text=None if knn_ok else req.query_text,
            ),
        }
        if knn_ok and qvec is not None:
            body["knn"] = {
                **_knn_clause(qvec, k=cfg.rrf_window),
                "filter": _filters(req),
            }

        resp = await self._es.search(index=index, body=body)
        total = int(resp["hits"]["total"]["value"])
        raw_hits = resp["hits"]["hits"]

        if total == 0:
            return SearchResponse(
                status=SearchStatus.NO_HIT,
                total=0,
                hits=[],
                effective_params=_effective(req),
            )

        if total > cfg.strict_max_hits:
            facets = await self._facet_counts(req)
            return SearchResponse(
                status=SearchStatus.TOO_MANY,
                total=total,
                hits=[],
                effective_params=_effective(req),
                facets=facets,
            )

        return SearchResponse(
            status=SearchStatus.STRICT_HIT,
            total=total,
            hits=[_hit_to_doc(h) for h in raw_hits[: req.size]],
            effective_params=_effective(req),
        )

    # ---------- loose ----------

    async def _loose(self, req: SearchRequest) -> SearchResponse:
        cfg = self._settings.search
        index = _index_for(req, self._settings.es.index_prefix)
        knn_ok = False
        if req.query_text:
            try:
                qvec = (await self._embedder.embed([req.query_text]))[0]
                knn_ok = True
            except (EmbeddingError, httpx.HTTPError):
                qvec = None
        else:
            qvec = None
        # When kNN is available skip the query_text BM25 boost (RRF handles ranking).
        # When kNN is down, inject query_text as a BM25 should-clause for better sentence recall.
        body: dict[str, Any] = {
            "size": req.size,
            "from": req.from_,
            "query": _bm25_query(
                req, "or", cfg.title_boost,
                query_text=None if knn_ok else req.query_text,
            ),
        }
        if knn_ok and qvec is not None:
            body["knn"] = {
                **_knn_clause(qvec, k=cfg.rrf_window),
                "filter": _filters(req),
            }
        resp = await self._es.search(index=index, body=body)
        total = int(resp["hits"]["total"]["value"])
        if total == 0:
            return SearchResponse(
                status=SearchStatus.NO_HIT,
                total=0,
                hits=[],
                effective_params=_effective(req),
            )
        return SearchResponse(
            status=SearchStatus.LOOSE_HIT,
            total=total,
            hits=[_hit_to_doc(h) for h in resp["hits"]["hits"]],
            effective_params=_effective(req),
            banner=LOOSE_BANNER,
        )

    # ---------- vector-only ----------

    async def _vector_only(self, req: SearchRequest) -> SearchResponse:
        if not req.query_text:
            return SearchResponse(
                status=SearchStatus.NO_HIT,
                total=0,
                hits=[],
                effective_params=_effective(req),
                banner=NO_HIT_BANNER,
            )
        cfg = self._settings.search
        index = _index_for(req, self._settings.es.index_prefix)
        try:
            qvec = (await self._embedder.embed([req.query_text]))[0]
        except (EmbeddingError, httpx.HTTPError):
            return SearchResponse(
                status=SearchStatus.NO_HIT,
                total=0,
                hits=[],
                effective_params=_effective(req),
                banner=NO_HIT_BANNER,
            )
        body: dict[str, Any] = {
            "size": req.size,
            "knn": {
                **_knn_clause(qvec, k=req.size),
                "filter": _filters(req),
            },
        }
        resp = await self._es.search(index=index, body=body)
        total = int(resp["hits"]["total"]["value"])
        if total == 0:
            return SearchResponse(
                status=SearchStatus.NO_HIT,
                total=0,
                hits=[],
                effective_params=_effective(req),
                banner=NO_HIT_BANNER,
            )
        return SearchResponse(
            status=SearchStatus.VECTOR_ONLY,
            total=total,
            hits=[_hit_to_doc(h) for h in resp["hits"]["hits"]],
            effective_params=_effective(req),
            banner=VECTOR_BANNER,
        )

    # ---------- facets ----------

    async def _facet_counts(self, req: SearchRequest) -> dict[str, dict[str, int]]:
        """Aggregation of project/equipment/error_codes for the strict-filtered
        result set. Used in TOO_MANY responses so the caller can ask the user
        which facet to narrow on.
        """
        cfg = self._settings.search
        index = _index_for(req, self._settings.es.index_prefix)
        body: dict[str, Any] = {
            "size": 0,
            "query": _bm25_query(req, "and", cfg.title_boost),
            "aggs": {
                "project": {"terms": {"field": "project", "size": 20}},
                "equipment": {"terms": {"field": "equipment", "size": 20}},
                "error_codes": {"terms": {"field": "error_codes", "size": 20}},
            },
        }
        resp = await self._es.search(index=index, body=body)
        out: dict[str, dict[str, int]] = {}
        for key in ("project", "equipment", "error_codes"):
            buckets = resp.get("aggregations", {}).get(key, {}).get("buckets", [])
            out[key] = {b["key"]: int(b["doc_count"]) for b in buckets}
        return out
