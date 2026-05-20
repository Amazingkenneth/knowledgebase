"""Thin HTTP client for the text-embeddings-inference (TEI) server.

Swapping to another provider (Qwen-Embedding, self-hosted vLLM with an OpenAI
shim, etc.) means rewriting only this file. Search/index code calls
embed(texts) and gets back list[list[float]] — no model-specific knowledge
leaks upstream.
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
        self._http = httpx.AsyncClient(base_url=cfg.url, timeout=cfg.timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        retry=retry_if_exception_type((httpx.HTTPError, EmbeddingError)),
        reraise=True,
    )
    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        # TEI /embed protocol: POST {"inputs": [str, ...]} -> [[float, ...], ...]
        resp = await self._http.post("/embed", json={"inputs": batch})
        if resp.status_code >= 500:
            raise EmbeddingError(f"embedding server {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            # 4xx — bad input, no retry value. Surface immediately as a permanent failure.
            raise EmbeddingError(f"embedding bad request {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not isinstance(data, list) or any(not isinstance(v, list) for v in data):
            raise EmbeddingError(f"embedding response shape invalid: {type(data).__name__}")
        for i, vec in enumerate(data):
            if len(vec) != self._cfg.dims:
                raise EmbeddingError(
                    f"embedding dim mismatch at row {i}: got {len(vec)}, expected {self._cfg.dims}"
                )
        return data

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
