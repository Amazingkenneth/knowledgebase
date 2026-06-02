"""The status→banner contract: every non-hit status must carry its banner.

Upstream callers render `banner` verbatim. A NO_HIT in strict/loose modes
previously returned `banner=None`, breaking the contract for direct
mode="strict"/"loose" callers (the auto pipeline masked it).
"""

import pytest

from kb.config import Settings
from kb.models.search import SearchRequest, SearchStatus
from kb.services.search import NO_HIT_BANNER, SearchService


class _FakeES:
    """Minimal async ES double that always reports zero hits."""

    async def search(self, *, index, body):  # noqa: ANN001
        return {"hits": {"total": {"value": 0}, "hits": []}}


def _service() -> SearchService:
    # embedder is unused when query_text is None (no rescore path).
    return SearchService(_FakeES(), Settings(), embedder=None)


@pytest.mark.parametrize("mode", ["strict", "loose", "auto", "vector_only"])
async def test_no_hit_always_carries_banner(mode: str):
    svc = _service()
    req = SearchRequest(keywords=["nonexistent"], mode=mode)
    resp = await svc.search(req)
    assert resp.status == SearchStatus.NO_HIT
    assert resp.banner == NO_HIT_BANNER
