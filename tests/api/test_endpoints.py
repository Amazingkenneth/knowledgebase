"""HTTP-level endpoint tests for the robustness/observability changes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from kb.models.search import EffectiveParams, SearchResponse, SearchStatus
from tests.api.conftest import FakeLLM, FakeSearch


def test_healthz_is_liveness_only(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_ok_when_es_reachable(make_client):
    with make_client(es_ping=True) as c:
        r = c.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["es"] == "ok"
    assert body["llm"] == "configured"


def test_readyz_503_when_es_down(make_client):
    with make_client(es_ping=False) as c:
        r = c.get("/readyz")
    assert r.status_code == 503
    assert r.json()["es"] == "down"


def test_request_id_echoed(client):
    r = client.get("/healthz")
    assert r.headers.get("X-Request-ID")


def test_metrics_exposes_prometheus_text(client):
    client.get("/healthz")  # generate at least one request metric
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "kb_http_requests_total" in r.text


def test_chat_flags_search_error_when_backend_raises(make_client):
    # Extraction returns sufficient params → search is attempted and raises.
    llm = FakeLLM(['{"keywords": ["pump", "leak"]}', "请稍后重试"])
    search = FakeSearch(error=RuntimeError("ES down"))
    with make_client(llm=llm, search=search) as c:
        r = c.post("/api/v1/chat", json={"messages": [{"role": "user", "content": "泵漏液"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["search_error"] is True
    assert search.called
    # The model was told retrieval is unavailable (degraded-search branch).
    final_system = llm.calls[-1][0]["content"]
    assert "检索服务暂时不可用" in final_system


def test_chat_happy_path_returns_results(make_client):
    hit_resp = SearchResponse(
        status=SearchStatus.STRICT_HIT,
        total=1,
        hits=[
            {
                "id": "1", "score": 1.0, "knowledge_type": "alarm",
                "project": "MEM", "equipment": "Pump", "error_codes": ["1030"],
                "title": "Pump leak",
            }
        ],
        effective_params=EffectiveParams(keywords=["pump", "leak"]),
    )
    llm = FakeLLM(['{"keywords": ["pump", "leak"]}', "根据检索结果…"])
    search = FakeSearch(response=hit_resp)
    with make_client(llm=llm, search=search) as c:
        r = c.post("/api/v1/chat", json={"messages": [{"role": "user", "content": "泵漏液"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["search_error"] is False
    assert body["search_status"] == "strict_hit"
    assert body["search_results"][0]["title"] == "Pump leak"


def test_chat_503_when_llm_not_configured(make_client):
    from kb.config import Settings

    cfg = Settings()
    cfg.llm.api_key = ""  # not configured

    class _Unconfigured(FakeLLM):
        @property
        def configured(self) -> bool:
            return False

        async def complete(self, messages, **_):
            from kb.services.llm import LLMNotConfiguredError

            raise LLMNotConfiguredError("no key")

    with make_client(settings=cfg, llm=_Unconfigured([])) as c:
        r = c.post("/api/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503


def test_commit_passes_vectors_skipped_through(make_client):
    pipeline = SimpleNamespace(
        commit_session=AsyncMock(
            return_value={"committed": 3, "skipped": 0, "errors": [], "vectors_skipped": 3}
        )
    )
    with make_client(pipeline=pipeline) as c:
        r = c.post("/api/v1/ingest/sessions/abc/commit")
    assert r.status_code == 200
    assert r.json()["vectors_skipped"] == 3
