"""Unit tests for collision detection and cross-referencing.

ES is stubbed so we exercise the pure logic: prospective-id computation, the
mget-driven collision lookup, exact intra-session grouping, and the related-docs
query shaping + exclusions. No live Elasticsearch or embedding service.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from kb.config import Settings
from kb.models.ingest import StagedDocument
from kb.models.taxonomy import KnowledgeType
from kb.services import cross_reference as cr
from kb.services.indexing import doc_id


def _alarm(index: int, *, title: str = "T", code: str = "E1001",
           project: str = "PDX", equipment: str = "Aligner") -> StagedDocument:
    return StagedDocument(
        index=index,
        knowledge_type=KnowledgeType.ALARM,
        title=title,
        project=project,
        equipment=equipment,
        error_codes=[code],
        content="c",
        resolution="r",
        source_file="f.pdf",
    )


# ── compute_staged_doc_id ─────────────────────────────────────────────────────

def test_staged_doc_id_matches_commit_id():
    """The prospective id must equal the id commit will assign, or the overwrite
    verdict would be wrong."""
    from kb.services.import_pipeline import _staged_to_knowledge_doc

    staged = _alarm(0)
    assert cr.compute_staged_doc_id(staged) == doc_id(_staged_to_knowledge_doc(staged))


# ── detect_collisions ─────────────────────────────────────────────────────────

async def test_detect_collisions_flags_existing_doc():
    staged = _alarm(0)
    did = cr.compute_staged_doc_id(staged)
    es = SimpleNamespace(mget=AsyncMock(return_value={"docs": [{
        "_id": did, "found": True, "_source": {
            "knowledge_type": "alarm", "title": "Existing", "project": "PDX",
            "equipment": "Aligner", "error_codes": ["E1001"],
            "sections": {"content": "old content", "resolution": "old res"},
            "updated_at": "2026-06-05T00:00:00+00:00",
        },
    }]}))
    await cr.detect_collisions(es, Settings(), [staged])

    assert staged.collision is not None
    assert staged.collision.title == "Existing"
    assert staged.collision.fields["content"] == "old content"
    assert staged.collision_action is None  # unresolved


async def test_detect_collisions_no_hit_leaves_doc_clean():
    staged = _alarm(0)
    es = SimpleNamespace(mget=AsyncMock(return_value={"docs": [{"found": False}]}))
    await cr.detect_collisions(es, Settings(), [staged])
    assert staged.collision is None


async def test_detect_collisions_disabled_is_noop():
    settings = Settings()
    settings.ingest.collision_detection_enabled = False
    es = SimpleNamespace(mget=AsyncMock())
    staged = _alarm(0)
    await cr.detect_collisions(es, settings, [staged])
    es.mget.assert_not_awaited()
    assert staged.collision is None


async def test_detect_collisions_groups_exact_intra_session():
    """Two staged docs with the same identity collide with each other, not the
    KB — they're folded into one exact-dup group instead of double-flagged."""
    a, b = _alarm(0), _alarm(1)  # identical identity
    a.confidence, b.confidence = 0.6, 0.9
    es = SimpleNamespace(mget=AsyncMock(return_value={"docs": [{"found": False}]}))
    await cr.detect_collisions(es, Settings(), [a, b])

    assert a.dup_group_id is not None and a.dup_group_id == b.dup_group_id
    # Highest-confidence is primary + stays accepted; the other is unchecked.
    assert b.dup_primary and b.accepted
    assert not a.dup_primary and not a.accepted


# ── find_related ──────────────────────────────────────────────────────────────

async def test_find_related_excludes_self_and_same_file():
    settings = Settings()
    settings.ingest.cross_reference_semantic = False  # exercise the BM25 path
    staged = _alarm(0)
    self_id = cr.compute_staged_doc_id(staged)
    es = SimpleNamespace(search=AsyncMock(return_value={"hits": {"hits": [
        {"_id": self_id, "_score": 9.0, "_source": {"title": "me", "source_file": "f.pdf"}},
        {"_id": "alarm:other", "_score": 5.0, "_source": {
            "knowledge_type": "alarm", "title": "Same file doc", "source_file": "f.pdf",
        }},
        {"_id": "alarm:keep", "_score": 4.0, "_source": {
            "knowledge_type": "alarm", "title": "Real related", "equipment": "Aligner",
            "error_codes": ["E1001"], "source_file": "other.pdf",
            "sections": {"content": "snippet text"},
        }},
    ]}}))
    await cr.find_related(es, settings, SimpleNamespace(), [staged])

    titles = [r.title for r in staged.related]
    assert titles == ["Real related"]  # self + same-file dropped
    assert staged.related[0].match_reason == "error_code"
    assert staged.related[0].snippet == "snippet text"


async def test_find_related_respects_cap():
    settings = Settings()
    settings.ingest.cross_reference_max = 2
    settings.ingest.cross_reference_semantic = False
    hits = [
        {"_id": f"alarm:{i}", "_score": float(10 - i), "_source": {
            "title": f"R{i}", "equipment": "Aligner", "source_file": "o.pdf",
        }}
        for i in range(5)
    ]
    es = SimpleNamespace(search=AsyncMock(return_value={"hits": {"hits": hits}}))
    staged = _alarm(0)
    await cr.find_related(es, settings, SimpleNamespace(), [staged])
    assert len(staged.related) == 2


async def test_find_related_disabled_is_noop():
    settings = Settings()
    settings.ingest.cross_reference_enabled = False
    es = SimpleNamespace(search=AsyncMock())
    staged = _alarm(0)
    await cr.find_related(es, settings, SimpleNamespace(), [staged])
    es.search.assert_not_awaited()
    assert staged.related == []
