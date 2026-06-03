"""HTTP-level endpoint test harness.

Builds the real FastAPI app via ``create_app`` but swaps in a lightweight
lifespan that injects fakes onto ``app.state`` — so we exercise the actual
routes, middleware, and serialization without booting Elasticsearch or seeding.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import kb.main as kb_main
from kb.config import Settings
from kb.main import create_app
from kb.models.taxonomy import KnowledgeType, Taxonomy


def _taxonomy() -> Taxonomy:
    return Taxonomy(
        version="test",
        knowledge_types=list(KnowledgeType),
        projects=["MEM", "所有项目"],
        equipment=["Pump", "Aligner"],
    )


class FakeLLM:
    """Returns queued responses in order; records the messages of each call."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    @property
    def configured(self) -> bool:
        return True

    async def complete(self, messages: list[dict[str, str]], **_: Any) -> str:
        self.calls.append(messages)
        return self._responses.pop(0) if self._responses else ""

    async def aclose(self) -> None:  # pragma: no cover - lifecycle no-op
        pass


class FakeSearch:
    """Search service double. Either returns a queued response or raises."""

    def __init__(self, response: Any = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.called = False

    async def search(self, req: Any) -> Any:
        self.called = True
        if self._error is not None:
            raise self._error
        return self._response


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch):
    """Factory: build a TestClient with injected fakes.

    Pass ``es_ping`` to control what /readyz sees; ``llm`` / ``search`` /
    ``pipeline`` / ``settings`` to override the defaults.
    """

    def _make(
        *,
        es_ping: bool = True,
        llm: Any = None,
        search: Any = None,
        pipeline: Any = None,
        settings: Settings | None = None,
        embedder: Any = None,
        raise_server_exceptions: bool = True,
    ) -> TestClient:
        cfg = settings or Settings()
        cfg.llm.api_key = "test-key"  # configured by default

        # /readyz (and any ES dep) goes through kb.main.get_es — fake the ping.
        fake_es = SimpleNamespace(ping=_ping_returning(es_ping))
        monkeypatch.setattr(kb_main, "get_es", lambda _settings: fake_es)

        @asynccontextmanager
        async def _lifespan(app: FastAPI):
            app.state.settings = cfg
            app.state.taxonomy_store = SimpleNamespace(current=_taxonomy())
            app.state.embedder = embedder if embedder is not None else SimpleNamespace()
            app.state.llm = llm or FakeLLM([])
            app.state.search = search or FakeSearch()
            app.state.import_pipeline = pipeline or SimpleNamespace()
            app.state.indexing = SimpleNamespace()
            yield

        app = create_app(lifespan_override=_lifespan)
        # raise_server_exceptions=False lets tests observe the 500 produced by
        # the catch-all exception handler instead of re-raising into the test.
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    return _make


def _ping_returning(value: bool):
    async def _ping() -> bool:
        return value

    return _ping


@pytest.fixture
def client(make_client) -> Iterator[TestClient]:
    with make_client() as c:
        yield c
