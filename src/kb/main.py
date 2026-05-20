from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from elasticsearch import NotFoundError
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from kb.api import chat, documents, facets, search
from kb.config import get_settings
from kb.es.client import close_es, get_es
from kb.es.mappings import alias_name
from kb.es.migrations import create_one
from kb.models.taxonomy import KnowledgeType
from kb.services.embedding import EmbeddingClient
from kb.services.indexing import IndexingService
from kb.services.search import SearchService
from kb.services.seed import seed_if_empty
from kb.services.taxonomy import TaxonomyStore

log = logging.getLogger("kb")

_FRONTEND_HTML = Path("Knowledge Base Search.html")


async def _ensure_indices(es, settings) -> None:
    """Create each alias+index if it doesn't already exist."""
    for kt in KnowledgeType:
        alias = alias_name(settings.es.index_prefix, kt)
        try:
            exists = await es.indices.exists_alias(name=alias)
            if exists:
                continue
        except Exception:
            pass
        try:
            name = await create_one(es, settings, kt)
            log.info("created index %s (alias %s)", name, alias)
        except Exception as exc:
            log.warning("could not create index for %s: %s", kt.value, exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    taxonomy_store = TaxonomyStore(settings.taxonomy.path)
    es = get_es(settings)
    embedder = EmbeddingClient(settings.embedding)

    app.state.settings = settings
    app.state.taxonomy_store = taxonomy_store
    app.state.embedder = embedder
    app.state.indexing = IndexingService(es, settings, embedder, taxonomy_store.current)
    app.state.search = SearchService(es, settings, embedder)

    # Auto-create indices and seed demo data on first run.
    await _ensure_indices(es, settings)
    await seed_if_empty(es, settings, embedder, taxonomy_store.current)

    log.info("kb up: taxonomy version=%s", taxonomy_store.current.version)
    try:
        yield
    finally:
        await embedder.aclose()
        await close_es()


def create_app() -> FastAPI:
    app = FastAPI(title="Knowledge Base", version="0.1.0", lifespan=lifespan)
    app.include_router(documents.router)
    app.include_router(search.router)
    app.include_router(facets.router)
    app.include_router(chat.router)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": exc.errors()},
        )

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def frontend() -> FileResponse:
        return FileResponse(_FRONTEND_HTML, media_type="text/html")

    return app


app = create_app()
