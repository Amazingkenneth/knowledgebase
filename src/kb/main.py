from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response

from elasticsearch import ApiError, AsyncElasticsearch, TransportError
from kb.api import chat, documents, facets, ingest, search
from kb.config import Settings, get_settings
from kb.es.client import close_es, get_es
from kb.es.import_mappings import IMPORT_INDEX_BODY, IMPORT_INDEX_NAME
from kb.es.mappings import alias_name, all_alias_pattern
from kb.es.migrations import create_one
from kb.models.taxonomy import KnowledgeType
from kb.observability import metrics
from kb.observability.logging_config import configure_logging, request_id_var
from kb.observability.middleware import MetricsMiddleware, RequestContextMiddleware
from kb.services.embedding import EmbeddingClient
from kb.services.import_pipeline import ImportPipeline
from kb.services.indexing import IndexingService
from kb.services.llm import LLMClient
from kb.services.search import SearchService
from kb.services.seed import restore_imports, seed
from kb.services.taxonomy import TaxonomyStore

log = logging.getLogger("kb")

_FRONTEND_HTML = Path("Knowledge Base Search.html")


def _request_id(request: Request) -> str:
    """Recover the current request id for an error body.

    Prefers the scope-backed ``request.state`` (set by RequestContextMiddleware and
    still readable in the outer ServerErrorMiddleware where the contextvar has
    already been reset), falling back to the contextvar, then ``-``.
    """
    rid = getattr(request.state, "request_id", None)
    return rid or request_id_var.get()


async def _probe_embedding(app: FastAPI) -> bool:
    """Best-effort reachability check for the embedding service (deep /readyz)."""
    embedder = getattr(app.state, "embedder", None)
    if embedder is None or not hasattr(embedder, "embed"):
        return False
    try:
        await embedder.embed(["ok"])
        return True
    except Exception as exc:  # noqa: BLE001 — probe must never raise
        log.warning("readyz deep probe: embedding unreachable — %s", exc)
        return False


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


async def _wait_for_es(es: AsyncElasticsearch, attempts: int = 5) -> bool:
    """Ping ES with a short exponential backoff so a slow-to-start cluster gives
    a clear log line instead of a stack trace mid-seed. Returns reachability."""
    delay = 0.5
    for attempt in range(1, attempts + 1):
        try:
            if await es.ping():
                return True
        except Exception as exc:  # noqa: BLE001 — connectivity probe
            log.warning("ES ping attempt %d/%d failed: %s", attempt, attempts, exc)
        if attempt < attempts:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 4.0)
    return False


async def _session_evictor(app: FastAPI, settings: Settings) -> None:
    """Periodically reclaim expired import sessions.

    Eviction otherwise only runs when a new upload arrives, so an idle server
    would pin abandoned preview sessions in memory indefinitely. Runs until the
    task is cancelled on shutdown.
    """
    interval = settings.ingest.session_evict_interval_minutes * 60
    while True:
        await asyncio.sleep(interval)
        pipeline = getattr(app.state, "import_pipeline", None)
        if pipeline is None:
            continue
        try:
            pipeline.evict_expired_sessions()
        except Exception as exc:  # noqa: BLE001 — sweeper must never die
            log.warning("session evictor: eviction failed — %s", exc)


async def _ensure_import_index(es) -> None:
    """Create the import file tracking index if it doesn't exist."""
    try:
        exists = await es.indices.exists(index=IMPORT_INDEX_NAME)
        if not exists:
            await es.indices.create(index=IMPORT_INDEX_NAME, body=IMPORT_INDEX_BODY)
            log.info("created import tracking index %s", IMPORT_INDEX_NAME)
    except Exception as exc:
        log.warning("could not create import index: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    taxonomy_store = TaxonomyStore(settings.taxonomy.path)
    es = get_es(settings)
    embedder = EmbeddingClient(settings.embedding)
    llm = LLMClient(settings.llm)

    app.state.settings = settings
    app.state.taxonomy_store = taxonomy_store
    app.state.embedder = embedder
    app.state.llm = llm
    app.state.indexing = IndexingService(es, settings, embedder, taxonomy_store.current)
    app.state.search = SearchService(es, settings, embedder)
    app.state.import_pipeline = ImportPipeline(
        es, settings, embedder, taxonomy_store.current, llm
    )

    # Surface degraded modes loudly so operators aren't left guessing.
    if not settings.llm.api_key:
        log.warning("KB_LLM__API_KEY not set — /chat and /extract will return 503.")
    if not settings.embedding.api_key:
        log.warning("KB_EMBEDDING__API_KEY not set — vector search disabled (BM25-only).")

    # Wait for ES before the (destructive) seed so a slow cluster doesn't crash
    # startup mid-reseed. If it never comes up, start degraded rather than hang.
    es_ok = await _wait_for_es(es)
    if es_ok:
        # Auto-create indices and reseed from CSV on every start.
        await _ensure_indices(es, settings)
        await _ensure_import_index(es)
        await seed(es, settings, embedder, taxonomy_store.current)

        # Re-index previously imported documents that were wiped by seed's clear.
        await restore_imports(es, settings)

        # Sync taxonomy with whatever values actually exist in ES, then rebuild
        # the services so they validate against the up-to-date taxonomy.
        await _sync_taxonomy_from_es(es, settings, taxonomy_store)
        app.state.indexing = IndexingService(es, settings, embedder, taxonomy_store.current)
        app.state.import_pipeline = ImportPipeline(
            es, settings, embedder, taxonomy_store.current, llm
        )
    else:
        log.error(
            "Elasticsearch unreachable at %s — starting in DEGRADED mode; "
            "search and indexing will fail until it recovers.", settings.es.url,
        )

    log.info("kb up: taxonomy version=%s, es=%s", taxonomy_store.current.version,
             "ok" if es_ok else "down")
    evict_task = asyncio.create_task(_session_evictor(app, settings))
    try:
        yield
    finally:
        evict_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await evict_task
        await embedder.aclose()
        await llm.aclose()
        await close_es()


def create_app(lifespan_override: object | None = None) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.observability)

    app = FastAPI(
        title="Knowledge Base",
        version="0.1.0",
        lifespan=lifespan_override or lifespan,  # type: ignore[arg-type]
    )

    # Outermost: request-id context (so every log line / inner metric carries it).
    app.add_middleware(RequestContextMiddleware)
    if settings.observability.metrics_enabled:
        app.add_middleware(MetricsMiddleware)

    app.include_router(documents.router)
    app.include_router(search.router)
    app.include_router(facets.router)
    app.include_router(chat.router)
    app.include_router(ingest.router)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": exc.errors()},
        )

    @app.exception_handler(TransportError)
    async def _es_transport_exc(request: Request, exc: TransportError) -> JSONResponse:
        """Elasticsearch is unreachable (connection refused / timeout) → 503.

        Without this, a transient ES outage surfaces to the client as an opaque,
        unlogged 500. Map it to a clear, logged 503 so callers (and orchestrators)
        can distinguish "backend down, retry" from a real server bug.
        """
        rid = _request_id(request)
        log.error("Elasticsearch transport error [%s]: %s", rid, exc, exc_info=exc)
        metrics.record_upstream_error("es")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "search backend unavailable", "request_id": rid},
        )

    @app.exception_handler(ApiError)
    async def _es_api_exc(request: Request, exc: ApiError) -> JSONResponse:
        """Elasticsearch reachable but rejected/failed the request → 502."""
        rid = _request_id(request)
        log.error("Elasticsearch API error [%s]: %s", rid, exc, exc_info=exc)
        metrics.record_upstream_error("es")
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "search backend error", "request_id": rid},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exc(request: Request, exc: Exception) -> JSONResponse:
        """Last-resort handler: never let an unforeseen error escape unlogged."""
        rid = _request_id(request)
        log.error("Unhandled error [%s]: %s", rid, exc, exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "internal server error", "request_id": rid},
        )

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up. Does not probe dependencies."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["meta"])
    async def readyz(deep: bool = Query(default=False)) -> JSONResponse:
        """Readiness: probe Elasticsearch and report subsystem availability.

        Returns 503 when ES is unreachable so an orchestrator / Docker
        healthcheck can route around or restart a degraded container.

        ``?deep=true`` additionally round-trips the embedding service (when
        configured) so the report reflects real reachability, not just whether
        a key is set. The default probe stays cheap (ES ping only) — deep checks
        cost an upstream call and shouldn't run on every healthcheck tick.
        """
        cfg = getattr(app.state, "settings", None) or get_settings()
        es = get_es(cfg)
        try:
            es_ok = bool(await es.ping())
        except Exception:  # noqa: BLE001 — probe must never raise
            es_ok = False

        if not cfg.embedding.api_key:
            embedding = "disabled"
        elif not deep:
            embedding = "configured"
        else:
            embedding = "ok" if await _probe_embedding(app) else "down"

        body = {
            "status": "ok" if es_ok else "degraded",
            "es": "ok" if es_ok else "down",
            "embedding": embedding,
            "llm": "configured" if cfg.llm.api_key else "disabled",
        }
        return JSONResponse(
            status_code=status.HTTP_200_OK if es_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body,
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> Response:
        if not get_settings().observability.metrics_enabled:
            return Response(status_code=status.HTTP_404_NOT_FOUND)
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    @app.get("/", include_in_schema=False)
    async def frontend() -> FileResponse:
        return FileResponse(_FRONTEND_HTML, media_type="text/html")

    return app


app = create_app()
