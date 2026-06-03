"""LLMClient retry / error-translation behavior, stubbed at the HTTP layer."""

from __future__ import annotations

from typing import Any

import pytest

from kb.config import LLMConfig
from kb.services.llm import LLMClient, LLMError, LLMNotConfiguredError


def _resp(status_code: int, content: str = "ok") -> Any:
    r = type("R", (), {})()
    r.status_code = status_code
    r.json = lambda: {"choices": [{"message": {"content": content}}]}
    r.text = ""
    return r


def _cfg(**kw: Any) -> LLMConfig:
    return LLMConfig(api_key="k", max_retries=kw.pop("max_retries", 2), **kw)


@pytest.mark.asyncio
async def test_not_configured_raises():
    client = LLMClient(LLMConfig(api_key=""))
    with pytest.raises(LLMNotConfiguredError):
        await client.complete([{"role": "user", "content": "hi"}])
    await client.aclose()


@pytest.mark.asyncio
async def test_retries_transient_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def fake_post(self, url, **kwargs):  # noqa: ARG001
        calls["n"] += 1
        # First call 503 (transient), second succeeds.
        return _resp(503 if calls["n"] == 1 else 200, "done")

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    client = LLMClient(_cfg(max_retries=2))
    out = await client.complete([{"role": "user", "content": "hi"}])
    assert out == "done"
    assert calls["n"] == 2  # one retry
    await client.aclose()


@pytest.mark.asyncio
async def test_permanent_400_not_retried(monkeypatch):
    calls = {"n": 0}

    async def fake_post(self, url, **kwargs):  # noqa: ARG001
        calls["n"] += 1
        return _resp(400)

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    client = LLMClient(_cfg(max_retries=3))
    with pytest.raises(LLMError):
        await client.complete([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1  # 4xx is permanent — no retries
    await client.aclose()
