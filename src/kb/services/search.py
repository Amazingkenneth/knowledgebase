"""Search service — the strict → loose → vector_only pipeline.

Two-stage retrieval design:
  - Recall stage  : keyword query (AND for strict, OR for loose) over `body`
                    with `title^N` boost. Filters are exact-match on keyword
                    fields; they never affect relevance, only inclusion.
  - Ranking stage : top `rrf_window` recall hits are rescored by blending the
                    BM25 score with cosine vector similarity on `body_vec`.
                    final_score = (1-vector_weight)*BM25 + vector_weight*(cos+1)
                    When the embedding service is unavailable the ranking stage
                    degrades gracefully to BM25-only (no rescore).
  - vector_only   : pure kNN (semantic-only, no keyword recall). Used as the
                    last fallback in the auto pipeline when keyword recall
                    returns nothing.
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
) -> dict[str, Any]:
    """Build the keyword recall `query` clause.

    - `filter` for Part-1 fields (exact, no scoring impact)
    - `must`   for keyword multi_match in the requested operator (AND / OR)
    - No keywords → match_all under filter (returns all docs for that scope)
    """
    bool_body: dict[str, Any] = {"filter": _filters(req)}
    if req.keywords:
        bool_body["must"] = [_kw_multi_match(req.keywords, kw_operator, title_boost)]
    else:
        bool_body["must"] = [{"match_all": {}}]
    return {"bool": bool_body}


def _rescore_clause(query_vec: list[float], window: int, vector_weight: float) -> dict[str, Any]:
    """Ranking-stage rescore: blend BM25 recall score with cosine vector similarity.

    Rescores only the top `window` keyword-recall hits.
    final_score = (1-vector_weight) * BM25 + vector_weight * (cosine_sim + 1)
    cosine_sim+1 maps [-1,1] → [0,2] so it is always non-negative.
    """
    kw_weight = 1.0 - vector_weight
    return {
        "window_size": window,
        "query": {
            "rescore_query": {
                "script_score": {
                    "query": {"match_all": {}},
                    "script": {
                        # Guard against docs that lack a body_vec (e.g. seeded without embeddings).
                        "source": (
                            "doc['body_vec'].size() == 0 ? 0 : "
                            "cosineSimilarity(params.qv, 'body_vec') + 1.0"
                        ),
                        "params": {"qv": query_vec},
                    },
                }
            },
            "query_weight": kw_weight,
            "rescore_query_weight": vector_weight,
            "score_mode": "total",
        },
    }


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
        summary=src.get("summary"),
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

        # Recall: AND-keyword BM25 query
        body: dict[str, Any] = {
            "size": max(req.size, cfg.strict_max_hits + 1),
            "from": req.from_,
            "query": _bm25_query(req, "and", cfg.title_boost),
        }

        # Ranking: rescore top candidates with BM25 + vector (when embedder is up)
        if req.query_text:
            try:
                qvec = (await self._embedder.embed([req.query_text]))[0]
                body["rescore"] = _rescore_clause(qvec, cfg.rrf_window, cfg.vector_weight)
            except (EmbeddingError, httpx.HTTPError):
                pass

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

        # Recall: OR-keyword BM25 query (any keyword match qualifies)
        body: dict[str, Any] = {
            "size": req.size,
            "from": req.from_,
            "query": _bm25_query(req, "or", cfg.title_boost),
        }

        # Ranking: rescore top candidates with BM25 + vector (when embedder is up)
        if req.query_text:
            try:
                qvec = (await self._embedder.embed([req.query_text]))[0]
                body["rescore"] = _rescore_clause(qvec, cfg.rrf_window, cfg.vector_weight)
            except (EmbeddingError, httpx.HTTPError):
                pass

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
