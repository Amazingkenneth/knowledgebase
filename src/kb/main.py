from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, UTC
from pathlib import Path
from typing import AsyncIterator

import yaml
from elasticsearch import AsyncElasticsearch, NotFoundError
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from kb.api import chat, documents, facets, search
from kb.config import Settings, get_settings
from kb.es.client import close_es, get_es
from kb.es.mappings import alias_name, all_alias_pattern
from kb.es.migrations import create_one
from kb.models.taxonomy import KnowledgeType
from kb.services.embedding import EmbeddingClient
from kb.services.indexing import IndexingService
from kb.services.search import SearchService
from kb.services.seed import seed
from kb.services.taxonomy import TaxonomyStore

log = logging.getLogger("kb")

_FRONTEND_HTML = Path("Knowledge Base Search.html")


async def _sync_taxonomy_from_es(
    es: AsyncElasticsearch, settings: Settings, taxonomy_store: TaxonomyStore
) -> None:
    """Discover project/equipment values in ES that are missing from taxonomy.yaml.

    Appends new values to taxonomy.yaml and reloads the store. Idempotent —
    safe to run on every startup even when no documents have changed.
    """
    try:
        resp = await es.search(
            index=all_alias_pattern(settings.es.index_prefix),
            body={
                "size": 0,
                "aggs": {
                    "projects":  {"terms": {"field": "project",   "size": 500}},
                    "equipment": {"terms": {"field": "equipment",  "size": 500}},
                },
            },
            ignore_unavailable=True,
        )
        aggs = resp.get("aggregations") or {}
        es_projects  = {b["key"] for b in aggs.get("projects",  {}).get("buckets", [])}
        es_equipment = {b["key"] for b in aggs.get("equipment", {}).get("buckets", [])}
    except Exception as exc:
        log.warning("taxonomy sync: could not query ES — %s", exc)
        return

    current       = taxonomy_store.current
    new_projects  = sorted(es_projects  - set(current.projects))
    new_equipment = sorted(es_equipment - set(current.equipment))

    if not new_projects and not new_equipment:
        log.debug("taxonomy sync: nothing new")
        return

    path = Path(settings.taxonomy.path)
    raw: dict = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if new_projects:
        raw["projects"] = raw.get("projects", []) + new_projects
        log.info("taxonomy sync: added projects %s", new_projects)
    if new_equipment:
        raw["equipment"] = raw.get("equipment", []) + new_equipment
        log.info("taxonomy sync: added equipment %s", new_equipment)
    raw["version"] = f"auto-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    path.write_text(
        yaml.dump(raw, allow_unicode=True, default_flow_style=False, sort_keys=True),
        encoding="utf-8",
    )
    taxonomy_store.reload()
    log.info(
        "taxonomy sync: reloaded — %d projects, %d equipment",
        len(raw.get("projects", [])),
        len(raw.get("equipment", [])),
    )


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

    # Auto-create indices and reseed from CSV on every start.
    await _ensure_indices(es, settings)
    await seed(es, settings, embedder, taxonomy_store.current)

    # Sync taxonomy with whatever project/equipment values actually exist in ES,
    # then rebuild the indexing service so it validates against the up-to-date taxonomy.
    await _sync_taxonomy_from_es(es, settings, taxonomy_store)
    app.state.indexing = IndexingService(es, settings, embedder, taxonomy_store.current)

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
