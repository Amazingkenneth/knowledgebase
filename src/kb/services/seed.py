"""Startup seeder — loads documents from CSV files in config/ and injects them on first run.

Idempotent: only runs for each index that has zero documents.
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


async def _index_count(es: AsyncElasticsearch, index: str) -> int:
    try:
        resp = await es.count(index=index)
        return int(resp["count"])
    except Exception:
        return 0


async def seed_if_empty(
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

    # Only seed indices that are currently empty.
    by_type: dict[KnowledgeType, list[KnowledgeDoc]] = {kt: [] for kt in KnowledgeType}
    for doc in all_docs:
        by_type[doc.knowledge_type].append(doc)

    needs_seeding: list[KnowledgeDoc] = []
    for kt, docs in by_type.items():
        alias = alias_name(settings.es.index_prefix, kt)
        count = await _index_count(es, alias)
        if count == 0 and docs:
            log.info("seed: %s is empty — injecting %d docs", alias, len(docs))
            needs_seeding.extend(docs)
        else:
            log.debug("seed: %s has %d docs — skipping", alias, count)

    if not needs_seeding:
        log.info("seed: all indices already populated")
        return

    # Try embeddings; fall back to BM25-only if TEI is unavailable.
    title_vecs: list[list[float] | None] = [None] * len(needs_seeding)
    body_vecs:  list[list[float] | None] = [None] * len(needs_seeding)
    try:
        titles   = [build_title_text(d) for d in needs_seeding]
        bodies   = [build_body(d)       for d in needs_seeding]
        all_vecs = await embedder.embed(titles + bodies)
        title_vecs = list(all_vecs[: len(needs_seeding)])
        body_vecs  = list(all_vecs[len(needs_seeding) :])
        log.info("seed: embeddings obtained for %d docs", len(needs_seeding))
    except (EmbeddingError, Exception) as exc:
        log.warning(
            "seed: embedding service unavailable (%s) — indexing without vectors; "
            "kNN search will be disabled until re-indexed with embeddings",
            exc,
        )

    now = datetime.now(UTC).isoformat()
    actions: list[dict[str, Any]] = []
    for i, doc in enumerate(needs_seeding):
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
