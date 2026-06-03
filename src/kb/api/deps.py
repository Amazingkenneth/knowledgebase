from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from elasticsearch import AsyncElasticsearch
from kb.config import Settings
from kb.es.client import get_es
from kb.services.embedding import EmbeddingClient
from kb.services.indexing import IndexingService
from kb.services.llm import LLMClient
from kb.services.search import SearchService
from kb.services.taxonomy import TaxonomyStore


def _es(request: Request) -> AsyncElasticsearch:
    return get_es(request.app.state.settings)


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _taxonomy_store(request: Request) -> TaxonomyStore:
    store: TaxonomyStore = request.app.state.taxonomy_store
    return store


def _embedder(request: Request) -> EmbeddingClient:
    embedder: EmbeddingClient = request.app.state.embedder
    return embedder


def _indexing(request: Request) -> IndexingService:
    svc: IndexingService = request.app.state.indexing
    svc.refresh_taxonomy(request.app.state.taxonomy_store.current)
    return svc


def _search(request: Request) -> SearchService:
    svc: SearchService = request.app.state.search
    return svc


def _llm(request: Request) -> LLMClient:
    llm: LLMClient = request.app.state.llm
    return llm


ESDep = Annotated[AsyncElasticsearch, Depends(_es)]
SettingsDep = Annotated[Settings, Depends(_settings)]
TaxonomyDep = Annotated[TaxonomyStore, Depends(_taxonomy_store)]
EmbedderDep = Annotated[EmbeddingClient, Depends(_embedder)]
IndexingDep = Annotated[IndexingService, Depends(_indexing)]
SearchDep = Annotated[SearchService, Depends(_search)]
LLMDep = Annotated[LLMClient, Depends(_llm)]
