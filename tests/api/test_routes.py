"""HTTP-level coverage for routes that previously had no endpoint tests:
/search, /facets, /extract, and the deep /readyz probe.
"""

from __future__ import annotations

from kb.models.search import EffectiveParams, SearchResponse, SearchStatus
from tests.api.conftest import FakeLLM, FakeSearch

# ── /search ──────────────────────────────────────────────────────────────────

def test_search_returns_service_response(make_client):
    resp = SearchResponse(
        status=SearchStatus.STRICT_HIT,
        total=1,
        hits=[
            {
                "id": "1", "score": 1.0, "knowledge_type": "alarm",
                "project": "MEM", "equipment": "Pump", "error_codes": ["1030"],
                "title": "Pump leak",
            }
        ],
        effective_params=EffectiveParams(keywords=["pump"]),
    )
    with make_client(search=FakeSearch(response=resp)) as c:
        r = c.post("/api/v1/search", json={"keywords": ["pump"]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "strict_hit"
    assert body["hits"][0]["title"] == "Pump leak"


# ── /facets ────────────────────────────────────────────────────────────────

def test_facets_returns_live_taxonomy(client):
    r = client.get("/api/v1/facets")
    assert r.status_code == 200
    body = r.json()
    assert "MEM" in body["projects"]
    assert "Pump" in body["equipment"]


# ── /extract ─────────────────────────────────────────────────────────────────

def test_extract_parses_llm_json(make_client):
    llm = FakeLLM(['{"project":"MEM","keywords":["leak"],"is_sentence":false}'])
    with make_client(llm=llm) as c:
        r = c.post("/api/v1/extract", json={"query": "MEM 泵漏液"})
    assert r.status_code == 200
    body = r.json()
    assert body["project"] == "MEM"
    assert body["keywords"] == ["leak"]


def test_extract_502_on_unparseable_llm_output(make_client):
    llm = FakeLLM(["not json at all"])
    with make_client(llm=llm) as c:
        r = c.post("/api/v1/extract", json={"query": "hi"})
    assert r.status_code == 502


# ── deep /readyz ─────────────────────────────────────────────────────────────

class _OkEmbedder:
    async def embed(self, texts):
        return [[0.0]] * len(texts)


class _DownEmbedder:
    async def embed(self, texts):
        raise RuntimeError("embedding down")


def test_readyz_deep_reports_embedding_ok(make_client):
    from kb.config import Settings

    cfg = Settings()
    cfg.embedding.api_key = "ek"
    with make_client(settings=cfg, embedder=_OkEmbedder()) as c:
        r = c.get("/readyz", params={"deep": "true"})
    assert r.status_code == 200
    assert r.json()["embedding"] == "ok"


def test_readyz_deep_reports_embedding_down(make_client):
    from kb.config import Settings

    cfg = Settings()
    cfg.embedding.api_key = "ek"
    with make_client(settings=cfg, embedder=_DownEmbedder()) as c:
        r = c.get("/readyz", params={"deep": "true"})
    # ES is still up, so overall readiness is 200; embedding flagged down.
    assert r.status_code == 200
    assert r.json()["embedding"] == "down"


def test_readyz_shallow_does_not_probe_embedding(make_client):
    from kb.config import Settings

    cfg = Settings()
    cfg.embedding.api_key = "ek"
    # _DownEmbedder would raise if probed; shallow readyz must not call it.
    with make_client(settings=cfg, embedder=_DownEmbedder()) as c:
        r = c.get("/readyz")
    assert r.status_code == 200
    assert r.json()["embedding"] == "configured"
