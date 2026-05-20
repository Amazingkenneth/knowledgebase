"""Indexing service: validate → build body → embed → index.

Validation runs against the live Taxonomy before anything else; a doc with an
unknown project is rejected before we waste an embedding call. Bulk requests
embed all docs in one batch and either index everything or return per-row
errors — partial indexing causes silent quality loss that is hard to detect
later (see plan, Section: Indexing service).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk

from kb.config import Settings
from kb.es.body_builder import build_body, build_title_text
from kb.es.mappings import alias_name
from kb.models.document import DocumentBase
from kb.models.taxonomy import Taxonomy
from kb.services.embedding import EmbeddingClient


class IndexingError(ValueError):
    """Raised for permanent (4xx-class) validation failures."""

    def __init__(self, message: str, *, row: int | None = None):
        super().__init__(message)
        self.row = row


def doc_id(doc: DocumentBase) -> str:
    """Stable, content-addressed ID for idempotent upserts.

    Same logical doc (same project+equipment+title+error_codes) re-indexes to
    the same _id. Editing notes/content updates the existing doc rather than
    creating a duplicate.
    """
    payload = "|".join(
        [
            doc.knowledge_type.value,
            doc.project,
            doc.equipment,
            doc.title.strip(),
            ",".join(sorted(doc.error_codes)),
        ]
    )
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{doc.knowledge_type.value}:{h}"


def validate_against_taxonomy(doc: DocumentBase, tax: Taxonomy, *, row: int | None = None) -> None:
    if doc.knowledge_type not in tax.knowledge_types:
        raise IndexingError(
            f"knowledge_type {doc.knowledge_type.value!r} not in taxonomy", row=row
        )
    if not tax.has_project(doc.project):
        raise IndexingError(f"project {doc.project!r} not in taxonomy", row=row)
    if not tax.has_equipment(doc.equipment):
        raise IndexingError(f"equipment {doc.equipment!r} not in taxonomy", row=row)


def _to_es_source(
    doc: DocumentBase,
    title_vec: list[float] | None,
    body_vec: list[float] | None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    source: dict[str, Any] = {
        "knowledge_type": doc.knowledge_type.value,
        "project": doc.project,
        "equipment": doc.equipment,
        "error_codes": doc.error_codes,
        "title": build_title_text(doc),
        "body": build_body(doc),
        "source_file": doc.source_file,
        "source_pages": doc.source_pages,
        "sections": dict(doc.content_sections()),
        "created_at": (doc.created_at or now).isoformat(),
        "updated_at": now.isoformat(),
    }
    # Vectors are optional — omit them when the embedding service is unavailable.
    # kNN search will skip docs without vectors; BM25 still works.
    if title_vec is not None:
        source["title_vec"] = title_vec
    if body_vec is not None:
        source["body_vec"] = body_vec
    return source


class IndexingService:
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

    def refresh_taxonomy(self, taxonomy: Taxonomy) -> None:
        self._taxonomy = taxonomy

    async def index_one(self, doc: DocumentBase) -> str:
        validate_against_taxonomy(doc, self._taxonomy)
        title_vec, body_vec = await self._embed_pair(build_title_text(doc), build_body(doc))
        _id = doc_id(doc)
        source = _to_es_source(doc, title_vec, body_vec)
        await self._es.index(
            index=alias_name(self._settings.es.index_prefix, doc.knowledge_type),
            id=_id,
            document=source,
            refresh="wait_for",
        )
        return _id

    async def index_bulk(self, docs: list[DocumentBase]) -> dict[str, Any]:
        """All-or-nothing-ish: validate every doc first, then index in one bulk.

        Validation errors short-circuit with per-row reporting. ES-side failures
        come back from async_bulk (we use stats_only=False to surface them).
        """
        if not docs:
            return {"indexed": 0, "errors": []}

        errors: list[dict[str, Any]] = []
        for i, doc in enumerate(docs):
            try:
                validate_against_taxonomy(doc, self._taxonomy, row=i)
            except IndexingError as e:
                errors.append({"row": i, "error": str(e)})
        if errors:
            return {"indexed": 0, "errors": errors}

        title_texts = [build_title_text(d) for d in docs]
        body_texts = [build_body(d) for d in docs]
        # Embed titles and bodies together to minimize HTTP round-trips.
        all_vecs = await self._embedder.embed(title_texts + body_texts)
        title_vecs = all_vecs[: len(docs)]
        body_vecs = all_vecs[len(docs) :]

        actions = []
        for doc, t_vec, b_vec in zip(docs, title_vecs, body_vecs, strict=True):
            actions.append(
                {
                    "_op_type": "index",
                    "_index": alias_name(self._settings.es.index_prefix, doc.knowledge_type),
                    "_id": doc_id(doc),
                    "_source": _to_es_source(doc, t_vec, b_vec),
                }
            )

        success, bulk_errors = await async_bulk(
            self._es, actions, raise_on_error=False, stats_only=False, refresh="wait_for"
        )
        for err in bulk_errors:
            # async_bulk returns the ES error dict per failed action.
            errors.append({"error": err})
        return {"indexed": success, "errors": errors}

    async def delete(self, knowledge_type: str, _id: str) -> bool:
        """Returns True if deleted, False if not found."""
        from kb.models.taxonomy import KnowledgeType

        kt = KnowledgeType(knowledge_type)
        try:
            await self._es.delete(
                index=alias_name(self._settings.es.index_prefix, kt),
                id=_id,
                refresh="wait_for",
            )
            return True
        except Exception as e:  # NotFoundError or similar
            if "not_found" in str(e).lower() or "NotFoundError" in type(e).__name__:
                return False
            raise

    async def _embed_pair(self, title: str, body: str) -> tuple[list[float], list[float]]:
        vecs = await self._embedder.embed([title, body])
        return vecs[0], vecs[1]
