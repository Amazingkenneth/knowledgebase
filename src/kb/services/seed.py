"""Startup seeder — loads documents from CSV files in config/ and reloads them on every start.

Always reseeds: clears all documents from each index then bulk-indexes from the CSV files.
This ensures the live index always reflects the current state of the CSV files — additions,
edits, and deletions all take effect on the next restart.

Graceful: if the embedding service is unavailable, documents are indexed
without vectors (BM25 search still works; kNN is enabled once re-indexed).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk

from kb.config import Settings
from kb.es.body_builder import build_body, build_title_text
from kb.es.mappings import alias_name
from kb.models.document import KnowledgeDoc
from kb.models.taxonomy import KnowledgeType, Taxonomy
from kb.services.csv_loader import load_csv_documents
from kb.services.embedding import EmbeddingClient, EmbeddingError
from kb.services.indexing import doc_id, validate_against_taxonomy

log = logging.getLogger("kb.seed")


async def _clear_index(es: AsyncElasticsearch, index: str) -> int:
    """Delete all documents from an index. Returns the count of deleted docs."""
    try:
        resp = await es.delete_by_query(
            index=index,
            body={"query": {"match_all": {}}},
            refresh=True,
        )
        deleted: int = int(resp.get("deleted", 0))
        return deleted
    except Exception as exc:
        log.warning("seed: could not clear %s — %s", index, exc)
        return 0


async def seed(
    es: AsyncElasticsearch,
    settings: Settings,
    embedder: EmbeddingClient,
    taxonomy: Taxonomy,
) -> None:
    # Load and validate all documents from the CSV files.
    all_docs: list[KnowledgeDoc] = []
    for doc in load_csv_documents():
        try:
            validate_against_taxonomy(doc, taxonomy)
            all_docs.append(doc)
        except Exception as exc:
            log.warning("seed: skipping %r — %s", doc.title, exc)

    if not all_docs:
        log.warning("seed: no documents loaded from CSV files — skipping")
        return

    # Clear each index before reseeding so deletions in the CSV propagate.
    for kt in KnowledgeType:
        alias = alias_name(settings.es.index_prefix, kt)
        deleted = await _clear_index(es, alias)
        log.info("seed: cleared %d docs from %s", deleted, alias)

    # Try embeddings; fall back to BM25-only if TEI is unavailable.
    title_vecs: list[list[float] | None] = [None] * len(all_docs)
    body_vecs:  list[list[float] | None] = [None] * len(all_docs)
    try:
        titles   = [build_title_text(d) for d in all_docs]
        bodies   = [build_body(d)       for d in all_docs]
        all_vecs = await embedder.embed(titles + bodies)
        title_vecs = list(all_vecs[: len(all_docs)])
        body_vecs  = list(all_vecs[len(all_docs) :])
        log.info("seed: embeddings obtained for %d docs", len(all_docs))
    except (EmbeddingError, Exception) as exc:
        log.warning(
            "seed: embedding service unavailable (%s) — indexing without vectors; "
            "kNN search will be disabled until re-indexed with embeddings",
            exc,
        )

    now = datetime.now(UTC).isoformat()
    actions: list[dict[str, Any]] = []
    for i, doc in enumerate(all_docs):
        source: dict[str, Any] = {
            "knowledge_type": doc.knowledge_type.value,
            "project":        doc.project,
            "equipment":      doc.equipment,
            "error_codes":    doc.error_codes,
            "title":          build_title_text(doc),
            "body":           build_body(doc),
            "source_file":    doc.source_file,
            "source_pages":   doc.source_pages,
            "sections":       dict(doc.content_sections()),
            "summary":        doc.summary,
            "created_at":     now,
            "updated_at":     now,
        }
        if title_vecs[i] is not None:
            source["title_vec"] = title_vecs[i]
        if body_vecs[i] is not None:
            source["body_vec"] = body_vecs[i]

        actions.append({
            "_op_type": "index",
            "_index":   alias_name(settings.es.index_prefix, doc.knowledge_type),
            "_id":      doc_id(doc),
            "_source":  source,
        })

    success, errors = await async_bulk(
        es, actions, raise_on_error=False, stats_only=False, refresh="wait_for"
    )
    for err in errors:
        log.error("seed bulk error: %s", err)
    log.info("seed: indexed %d documents (%d errors)", success, len(errors))
