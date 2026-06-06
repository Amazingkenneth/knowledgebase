"""Collision detection and cross-referencing for staged import documents.

Two read-only ES enrichments run after segmentation, before the reviewer sees
the documents:

- ``detect_collisions`` — a staged doc's content-addressed ``doc_id`` already
  exists in the live ``kb_<type>`` index, so committing it would *overwrite* a
  committed KB doc. We attach a snapshot of the existing doc and leave the
  collision unresolved (commit is blocked until the reviewer decides). Staged
  docs that collide with *each other* in the same batch are folded into an
  exact-duplicate group instead.

- ``find_related`` — committed KB docs related to a staged one by shared error
  code, equipment, or (when embeddings are available) semantic similarity, so
  the reviewer sees existing coverage of the same problem before importing yet
  another copy.

Both are best-effort: any ES/embedding failure is swallowed and simply yields
no enrichment — they must never block the import.
"""

from __future__ import annotations

import logging
from typing import Any

from elasticsearch import AsyncElasticsearch
from kb.config import Settings
from kb.es.mappings import alias_name, all_alias_pattern
from kb.models.ingest import ExistingDocSnapshot, RelatedDoc, StagedDocument
from kb.services.embedding import EmbeddingClient, EmbeddingError
from kb.services.indexing import doc_id

log = logging.getLogger("kb.cross_reference")

# Per-doc related-doc snippet length — enough to recognise the item, short
# enough to keep the session payload small.
_SNIPPET_CHARS = 160


def compute_staged_doc_id(staged: StagedDocument) -> str | None:
    """Prospective ES ``_id`` for a staged doc — byte-identical to what commit
    will assign — or None if the doc can't yet be built into a valid KnowledgeDoc.

    Reuses the same conversion the commit path uses (lazy import to avoid a
    circular dependency with import_pipeline), so a "would overwrite" verdict
    here matches the overwrite that would actually happen on commit.
    """
    from kb.services.import_pipeline import _staged_to_knowledge_doc

    try:
        return doc_id(_staged_to_knowledge_doc(staged))
    except Exception:  # invalid/empty staged doc — can't collide yet
        return None


def _snippet_from_source(src: dict[str, Any]) -> str:
    sections = src.get("sections")
    if isinstance(sections, dict):
        for value in sections.values():
            if isinstance(value, str) and value.strip():
                return value.strip()[:_SNIPPET_CHARS]
    body = src.get("body")
    if isinstance(body, str):
        return body.strip()[:_SNIPPET_CHARS]
    return ""


def _snapshot_from_source(_id: str, src: dict[str, Any]) -> ExistingDocSnapshot:
    sections = src.get("sections")
    fields = {k: v for k, v in sections.items() if isinstance(v, str)} if isinstance(
        sections, dict
    ) else {}
    return ExistingDocSnapshot(
        doc_id=_id,
        knowledge_type=str(src.get("knowledge_type", "")),
        project=str(src.get("project", "")),
        equipment=str(src.get("equipment", "")),
        title=str(src.get("title", "")),
        error_codes=list(src.get("error_codes", []) or []),
        fields=fields,
        source_file=src.get("source_file"),
        source_pages=list(src.get("source_pages", []) or []),
        updated_at=src.get("updated_at"),
    )


def _group_exact_intra_session(
    staged_docs: list[StagedDocument],
) -> tuple[set[int], dict[int, str | None]]:
    """Fold staged docs that share a prospective ``doc_id`` into one exact-dup
    group (only one would survive commit otherwise — the later overwrites the
    earlier). Highest confidence becomes the primary; the rest are unchecked.

    Returns ``(suppressed, id_cache)`` where ``suppressed`` is the set of doc
    indices that are non-primary members of an exact group (the caller skips KB
    collision lookups for those — they collide with a sibling, not the KB) and
    ``id_cache`` maps each doc index to its computed prospective id.
    """
    by_id: dict[str, list[StagedDocument]] = {}
    id_cache: dict[int, str | None] = {}
    for d in staged_docs:
        did = compute_staged_doc_id(d)
        id_cache[d.index] = did
        if did is not None:
            by_id.setdefault(did, []).append(d)

    suppressed: set[int] = set()
    for did, group in by_id.items():
        if len(group) < 2:
            continue
        # Don't clobber a group already assigned by the segmenter's fuzzy dedup.
        ordered = sorted(group, key=lambda d: d.confidence, reverse=True)
        primary = ordered[0]
        for d in group:
            if d.dup_group_id is None:
                d.dup_group_id = f"id:{did}"
            d.dup_primary = d is primary
            if d is not primary:
                d.accepted = False
                suppressed.add(d.index)
    return suppressed, id_cache


async def detect_collisions(
    es: AsyncElasticsearch,
    settings: Settings,
    staged_docs: list[StagedDocument],
) -> None:
    """Mark staged docs whose commit would overwrite an existing KB doc.

    Mutates ``staged_docs`` in place: sets ``collision`` (and leaves
    ``collision_action=None``) on each doc whose prospective ``doc_id`` is
    already present in the live index. Exact intra-session duplicates are
    grouped rather than flagged as KB collisions.
    """
    if not settings.ingest.collision_detection_enabled or not staged_docs:
        return

    suppressed, id_cache = _group_exact_intra_session(staged_docs)

    # Gather one mget per alias index, keyed by prospective id.
    by_index: dict[str, dict[str, StagedDocument]] = {}
    for d in staged_docs:
        if d.index in suppressed:
            continue
        did = id_cache.get(d.index)
        if not did:
            continue
        alias = alias_name(settings.es.index_prefix, d.knowledge_type)
        # First staged doc wins the id within an index; later ones are exact
        # siblings already handled by grouping.
        by_index.setdefault(alias, {}).setdefault(did, d)

    for alias, id_map in by_index.items():
        ids = list(id_map.keys())
        try:
            resp = await es.mget(index=alias, ids=ids)
        except Exception as exc:  # index missing / ES blip — skip silently
            log.debug("collision mget failed for %s: %s", alias, exc)
            continue
        for entry in resp.get("docs", []):
            if not entry.get("found"):
                continue
            _id = entry.get("_id", "")
            staged = id_map.get(_id)
            if staged is None:
                continue
            staged.collision = _snapshot_from_source(_id, entry.get("_source", {}) or {})


def _related_text(staged: StagedDocument) -> str:
    parts = [staged.title, staged.content, staged.resolution, staged.procedure,
             staged.prerequisites, staged.body_text]
    return "\n".join(p for p in parts if p and p != "—").strip()


def _match_reason(
    staged: StagedDocument, src: dict[str, Any]
) -> str:
    staged_codes = {c.upper() for c in staged.error_codes}
    hit_codes = {str(c).upper() for c in (src.get("error_codes") or [])}
    if staged_codes and staged_codes & hit_codes:
        return "error_code"
    if staged.equipment and src.get("equipment") == staged.equipment:
        return "equipment"
    return "similar"


async def find_related(
    es: AsyncElasticsearch,
    settings: Settings,
    embedder: EmbeddingClient,
    staged_docs: list[StagedDocument],
) -> None:
    """Attach related committed KB docs to each staged doc (best-effort).

    Matches by shared error code or equipment, plus BM25/kNN similarity on the
    title+body text. Excludes the staged doc's own prospective id and docs that
    came from the same source file. Capped at ``ingest.cross_reference_max``.
    """
    if not settings.ingest.cross_reference_enabled or not staged_docs:
        return

    cap = settings.ingest.cross_reference_max
    pattern = all_alias_pattern(settings.es.index_prefix)

    # One batched embed for the whole session when semantic search is enabled
    # and an embedding key is configured. Failures fall back to BM25-only.
    use_semantic = settings.ingest.cross_reference_semantic and bool(
        settings.embedding.api_key
    )
    vectors: dict[int, list[float]] = {}
    if use_semantic:
        texts = [_related_text(d) for d in staged_docs]
        try:
            embedded = await embedder.embed([t or " " for t in texts])
            vectors = {d.index: embedded[i] for i, d in enumerate(staged_docs)}
        except (EmbeddingError, OSError, RuntimeError) as exc:
            log.debug("related-docs embedding failed, BM25-only: %s", exc)

    for staged in staged_docs:
        self_id = compute_staged_doc_id(staged)
        text = _related_text(staged)
        should: list[dict[str, Any]] = []
        if staged.error_codes:
            should.append({"terms": {"error_codes": staged.error_codes}})
        if staged.equipment:
            should.append({"term": {"equipment": staged.equipment}})
        if text:
            should.append({
                "multi_match": {
                    "query": text[:512],
                    "fields": ["title^3", "body"],
                }
            })
        if not should:
            continue

        body: dict[str, Any] = {
            "size": cap + 1,  # +1 to absorb a self-hit before trimming
            "query": {"bool": {"should": should, "minimum_should_match": 1}},
            "_source": [
                "knowledge_type", "title", "equipment", "error_codes",
                "source_file", "sections", "body",
            ],
        }
        knn = None
        if staged.index in vectors:
            knn = {
                "field": "body_vec",
                "query_vector": vectors[staged.index],
                "k": cap + 1,
                "num_candidates": max(50, (cap + 1) * 10),
            }
        try:
            if knn is not None:
                resp = await es.search(index=pattern, knn=knn, **body)
            else:
                resp = await es.search(index=pattern, **body)
        except Exception as exc:
            log.debug("related-docs search failed for doc %d: %s", staged.index, exc)
            continue

        related: list[RelatedDoc] = []
        for hit in resp.get("hits", {}).get("hits", []):
            hid = hit.get("_id", "")
            src = hit.get("_source", {}) or {}
            # Skip the staged doc's own prospective doc and same-source-file docs.
            if self_id and hid == self_id:
                continue
            if staged.source_file and src.get("source_file") == staged.source_file:
                continue
            related.append(RelatedDoc(
                doc_id=hid,
                knowledge_type=str(src.get("knowledge_type", "")),
                title=str(src.get("title", "")),
                equipment=str(src.get("equipment", "")),
                error_codes=list(src.get("error_codes", []) or []),
                source_file=src.get("source_file"),
                match_reason=_match_reason(staged, src),
                score=float(hit.get("_score") or 0.0),
                snippet=_snippet_from_source(src),
            ))
            if len(related) >= cap:
                break
        staged.related = related
