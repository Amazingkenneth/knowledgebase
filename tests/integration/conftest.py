"""Shared fixtures for integration tests.

These require a running Elasticsearch with the IK analyzer plugin installed.
By default they spin up a container via testcontainers; if that fails (no
Docker), the tests are skipped instead of erroring.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

pytest.importorskip("testcontainers")

from testcontainers.elasticsearch import ElasticSearchContainer  # noqa: E402

from kb.config import EmbeddingConfig, ESConfig, SearchConfig, Settings, TaxonomyConfig  # noqa: E402
from kb.es.client import close_es, get_es  # noqa: E402
from kb.es.migrations import create_all  # noqa: E402
from kb.models.taxonomy import KnowledgeType, Taxonomy  # noqa: E402


@pytest.fixture(scope="session")
def es_container():
    try:
        # IK plugin install is non-trivial in a vanilla image; tests that need
        # IK-specific tokenization should mark themselves and use a dedicated
        # image. For now we run with the default analyzer for shape tests.
        with ElasticSearchContainer("docker.elastic.co/elasticsearch/elasticsearch:8.15.3") as es:
            yield es
    except Exception as e:
        pytest.skip(f"could not start ES container: {e}")


@pytest.fixture
def settings(es_container):
    return Settings(
        es=ESConfig(url=es_container.get_url(), index_prefix="kbtest"),
        embedding=EmbeddingConfig(url="http://localhost:9", dims=8, batch_size=4),
        search=SearchConfig(strict_max_hits=3),
        taxonomy=TaxonomyConfig(path="config/taxonomy.yaml"),
    )


@pytest_asyncio.fixture
async def fresh_indices(settings):
    es = get_es(settings)
    # Best-effort cleanup of any prior test run.
    try:
        await es.indices.delete(index=f"{settings.es.index_prefix}_*", ignore_unavailable=True)
    except Exception:
        pass
    await create_all(es, settings)
    yield
    await close_es()


@pytest.fixture
def fake_taxonomy() -> Taxonomy:
    return Taxonomy(
        version="test",
        knowledge_types=[KnowledgeType.ALARM, KnowledgeType.SETUP, KnowledgeType.EXPERIENCE],
        projects=["MHK", "MEM", "FTU"],
        equipment=["Sphere", "FTU", "LDI"],
    )
