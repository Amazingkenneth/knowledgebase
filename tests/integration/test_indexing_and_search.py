"""Integration smoke tests against a real ES container with a stub embedder.

Marked `integration` — skip when Docker is unavailable. The embedder is
replaced with a deterministic stub so we don't need a real embedding server
in CI; the search-quality concerns these tests check are about BM25 + filter
behavior, not vector ranking.
"""

from __future__ import annotations

import pytest

from kb.es.client import get_es
from kb.models.document import AlarmDoc
from kb.models.search import SearchRequest, SearchStatus
from kb.models.taxonomy import KnowledgeType
from kb.services.indexing import IndexingService
from kb.services.search import SearchService

pytestmark = pytest.mark.integration


class StubEmbedder:
    def __init__(self, dims: int):
        self._dims = dims

    async def embed(self, texts):
        # Hash-based deterministic pseudo-vector. Sufficient to satisfy mapping
        # constraints and prove vector_only and hybrid paths execute.
        return [[(hash((t, i)) % 1000) / 1000.0 for i in range(self._dims)] for t in texts]

    async def aclose(self):
        pass


def _alarm(**overrides) -> AlarmDoc:
    base = dict(
        project="FTU",
        equipment="Sphere",
        error_codes=["125002"],
        title="穿梭真空感应失败",
        content="穿梭真空报警分为两种",
        resolution="确认对应报警穿梭穴位",
    )
    base.update(overrides)
    return AlarmDoc(**base)


async def test_strict_hit(settings, fresh_indices, fake_taxonomy):
    es = get_es(settings)
    embedder = StubEmbedder(settings.embedding.dims)
    indexing = IndexingService(es, settings, embedder, fake_taxonomy)
    search = SearchService(es, settings, embedder)

    await indexing.index_one(_alarm(error_codes=["125002"]))
    await indexing.index_one(_alarm(error_codes=["999999"], title="x"))

    resp = await search.search(
        SearchRequest(
            knowledge_type=KnowledgeType.ALARM,
            project="FTU",
            error_codes=["125002"],
        )
    )
    assert resp.status == SearchStatus.STRICT_HIT
    assert resp.total == 1
    assert resp.hits[0].title == "穿梭真空感应失败"


async def test_too_many_triggers_facets(settings, fresh_indices, fake_taxonomy):
    es = get_es(settings)
    embedder = StubEmbedder(settings.embedding.dims)
    indexing = IndexingService(es, settings, embedder, fake_taxonomy)
    search = SearchService(es, settings, embedder)

    for i in range(settings.search.strict_max_hits + 2):
        await indexing.index_one(_alarm(title=f"alarm-{i}", error_codes=[f"E{i}"]))

    resp = await search.search(
        SearchRequest(knowledge_type=KnowledgeType.ALARM, project="FTU")
    )
    assert resp.status == SearchStatus.TOO_MANY
    assert resp.facets["project"]["FTU"] >= settings.search.strict_max_hits + 1


async def test_no_hit_when_nothing_matches(settings, fresh_indices, fake_taxonomy):
    es = get_es(settings)
    embedder = StubEmbedder(settings.embedding.dims)
    indexing = IndexingService(es, settings, embedder, fake_taxonomy)
    search = SearchService(es, settings, embedder)

    await indexing.index_one(_alarm())
    resp = await search.search(
        SearchRequest(
            knowledge_type=KnowledgeType.ALARM,
            project="MEM",  # filter excludes all docs
            keywords=["nonsense"],
        )
    )
    assert resp.status == SearchStatus.NO_HIT
    assert resp.hits == []
    assert resp.banner is not None


async def test_error_code_exact_match_disambiguation(settings, fresh_indices, fake_taxonomy):
    """XYZ123456 must not recall XYZ123457 — they're different alarms."""
    es = get_es(settings)
    embedder = StubEmbedder(settings.embedding.dims)
    indexing = IndexingService(es, settings, embedder, fake_taxonomy)
    search = SearchService(es, settings, embedder)

    await indexing.index_one(_alarm(error_codes=["XYZ123456"], title="温度传感器故障"))
    await indexing.index_one(_alarm(error_codes=["XYZ123457"], title="液压泵过载"))

    resp = await search.search(
        SearchRequest(knowledge_type=KnowledgeType.ALARM, error_codes=["XYZ123456"])
    )
    assert resp.status == SearchStatus.STRICT_HIT
    assert resp.total == 1
    assert resp.hits[0].title == "温度传感器故障"


async def test_effective_params_echo(settings, fresh_indices, fake_taxonomy):
    es = get_es(settings)
    embedder = StubEmbedder(settings.embedding.dims)
    indexing = IndexingService(es, settings, embedder, fake_taxonomy)
    search = SearchService(es, settings, embedder)
    await indexing.index_one(_alarm())

    resp = await search.search(
        SearchRequest(
            knowledge_type=KnowledgeType.ALARM,
            project="FTU",
            equipment="Sphere",
            error_codes=["125002"],
            keywords=["真空"],
        )
    )
    ep = resp.effective_params
    assert ep.project == "FTU"
    assert ep.equipment == "Sphere"
    assert ep.error_codes == ["125002"]
    assert ep.keywords == ["真空"]
