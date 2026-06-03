"""Shared HTTP client for OpenAI-compatible chat-completions APIs (e.g. DashScope).

One persistent client (connection reuse) with transient-failure retries, mirroring
``services/embedding.py``. Used by both the chat/extract endpoints and the import
segmenter so retry/timeout behavior lives in exactly one place.
"""

from __future__ import annotations

import logging

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from kb.config import LLMConfig
from kb.observability import metrics

log = logging.getLogger("kb.llm")


class LLMNotConfiguredError(RuntimeError):
    """Raised when no API key is set — the caller should surface HTTP 503."""


class LLMError(RuntimeError):
    """Permanent LLM failure (after retries for transient errors)."""


class _TransientLLMError(LLMError):
    """Retryable upstream failure (HTTP 429 / 5xx)."""


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self._cfg = cfg
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self._http = httpx.AsyncClient(headers=headers, timeout=cfg.timeout_s)

    @property
    def configured(self) -> bool:
        return bool(self._cfg.api_key)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(
        self,
        messages: list[dict[str, str]],
        timeout: float | None,
        max_tokens: int | None,
    ) -> str:
        resp = await self._http.post(
            self._cfg.api_url,
            json={
                "model": self._cfg.model,
                "messages": messages,
                "max_tokens": max_tokens or self._cfg.max_tokens,
                "stream": False,
            },
            timeout=timeout if timeout is not None else self._cfg.timeout_s,
        )
        # 429 (rate limit) and 5xx (overload/outage) are worth retrying;
        # other 4xx are permanent client errors and must not be retried.
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _TransientLLMError(f"LLM upstream {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            raise LLMError(f"LLM upstream {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            content: str = data["choices"][0]["message"]["content"]
            return content
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM upstream returned an unparseable response: {exc}") from exc

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Call the chat-completions API, retrying transient failures.

        Raises ``LLMNotConfiguredError`` when no key is set, or ``LLMError`` on a
        permanent failure (bad request, unparseable body, or transient errors
        that outlived the retry budget).
        """
        if not self._cfg.api_key:
            raise LLMNotConfiguredError("LLM not configured — set KB_LLM__API_KEY")
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._cfg.max_retries + 1),
                wait=wait_exponential(multiplier=0.5, max=4),
                retry=retry_if_exception_type((httpx.HTTPError, _TransientLLMError)),
                reraise=True,
            ):
                with attempt:
                    return await self._post(messages, timeout, max_tokens)
        except httpx.HTTPError as exc:
            metrics.record_upstream_error("llm")
            raise LLMError(f"LLM transport error: {exc}") from exc
        except LLMError:
            metrics.record_upstream_error("llm")
            raise
        # AsyncRetrying always either returns from the with-block or raises.
        raise LLMError("LLM call did not complete")  # pragma: no cover
