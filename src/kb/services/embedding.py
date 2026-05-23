"""HTTP client for OpenAI-compatible text-embeddings APIs (e.g. DashScope).

Swapping providers means updating EmbeddingConfig only — no code changes here.
Search/index code calls embed(texts) and gets back list[list[float]].
"""

from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from kb.config import EmbeddingConfig


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(self, cfg: EmbeddingConfig):
        self._cfg = cfg
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        # httpx joins base_url + path like urljoin: a leading "/" on the request
        # path replaces the base path, so we normalize by ensuring a trailing
        # slash on base_url and using a relative request path below.
        base = cfg.url if cfg.url.endswith("/") else cfg.url + "/"
        self._http = httpx.AsyncClient(
            base_url=base,
            headers=headers,
            timeout=cfg.timeout_s,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        retry=retry_if_exception_type((httpx.HTTPError, EmbeddingError)),
        reraise=True,
    )
    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        # OpenAI-compatible embeddings protocol:
        # POST /embeddings  {"model": "...", "input": [...]}
        # -> {"data": [{"index": i, "embedding": [float, ...]}, ...]}
        resp = await self._http.post(
            "embeddings",
            json={"model": self._cfg.model, "input": batch},
        )
        if resp.status_code >= 500:
            raise EmbeddingError(f"embedding server {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            raise EmbeddingError(f"embedding bad request {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        try:
            # Sort by index to guarantee input order is preserved
            items = sorted(data["data"], key=lambda x: x["index"])
            vectors = [item["embedding"] for item in items]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError(f"embedding response shape invalid: {exc}") from exc
        for i, vec in enumerate(vectors):
            if len(vec) != self._cfg.dims:
                raise EmbeddingError(
                    f"embedding dim mismatch at row {i}: got {len(vec)}, expected {self._cfg.dims}"
                )
        return vectors

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed `texts` in deterministic input order. Empty list returns [].

        Raises EmbeddingError on permanent failure (after retries for transient errors).
        """
        if not texts:
            return []
        out: list[list[float]] = []
        bs = self._cfg.batch_size
        for i in range(0, len(texts), bs):
            chunk = texts[i : i + bs]
            out.extend(await self._embed_batch(chunk))
        return out
