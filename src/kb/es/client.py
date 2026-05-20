from __future__ import annotations

from elasticsearch import AsyncElasticsearch

from kb.config import Settings

_client: AsyncElasticsearch | None = None


def get_es(settings: Settings) -> AsyncElasticsearch:
    """Process-wide AsyncElasticsearch singleton.

    FastAPI lifespan calls close_es() on shutdown.
    Supports HTTPS + basic auth + TLS fingerprint as configured in ESConfig.
    """
    global _client
    if _client is None:
        kwargs: dict = {"request_timeout": settings.es.request_timeout_s}

        cfg = settings.es
        if cfg.username and cfg.password:
            kwargs["basic_auth"] = (cfg.username, cfg.password)

        if cfg.ssl_fingerprint:
            # Pinned fingerprint — no CA bundle needed, works with self-signed certs.
            kwargs["ssl_assert_fingerprint"] = cfg.ssl_fingerprint
        elif not cfg.verify_certs:
            kwargs["verify_certs"] = False

        _client = AsyncElasticsearch(cfg.url, **kwargs)
    return _client


async def close_es() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
