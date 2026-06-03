"""Unit tests for the embedding HTTP client: batching, input-order guarantee,
dimension validation, and transient-failure retry.
"""

from __future__ import annotations

import httpx
import pytest

from kb.config import EmbeddingConfig
from kb.services.embedding import EmbeddingClient, EmbeddingError

pytestmark = pytest.mark.asyncio

DIMS = 3


def _client(handler, *, batch_size: int = 10) -> EmbeddingClient:
    cfg = EmbeddingConfig(
        url="http://emb.test", api_key="k", model="m", dims=DIMS, batch_size=batch_size
    )
    client = EmbeddingClient(cfg)
    # Swap the real transport for an in-memory mock; keep base_url/headers.
    client._http = httpx.AsyncClient(
        base_url="http://emb.test/",
        transport=httpx.MockTransport(handler),
    )
    return client


def _vec(seed: int) -> list[float]:
    return [float(seed)] * DIMS


async def test_empty_input_returns_empty():
    client = _client(lambda req: httpx.Response(500))
    try:
        assert await client.embed([]) == []
    finally:
        await client.aclose()


async def test_preserves_input_order_across_batches():
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        inputs = json.loads(request.content)["input"]
        calls.append(inputs)
        # Return embeddings deliberately out of order to prove we re-sort by index.
        data = [{"index": len(inputs) - 1 - i, "embedding": _vec(len(inputs) - 1 - i)}
                for i in range(len(inputs))]
        return httpx.Response(200, json={"data": data})

    client = _client(handler, batch_size=2)
    try:
        out = await client.embed(["a", "b", "c"])
    finally:
        await client.aclose()

    assert len(out) == 3
    # Two batches: ["a","b"] then ["c"].
    assert calls == [["a", "b"], ["c"]]
    # Within each batch, output is sorted back to input order.
    assert out[0] == _vec(0) and out[1] == _vec(1)


async def test_dim_mismatch_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    client = _client(handler)
    try:
        with pytest.raises(EmbeddingError, match="dim mismatch"):
            await client.embed(["x"])
    finally:
        await client.aclose()


async def test_bad_request_raises():
    client = _client(lambda req: httpx.Response(400, text="nope"))
    try:
        with pytest.raises(EmbeddingError, match="bad request"):
            await client.embed(["x"])
    finally:
        await client.aclose()


async def test_transient_5xx_is_retried_then_succeeds():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": _vec(7)}]})

    client = _client(handler)
    try:
        out = await client.embed(["x"])
    finally:
        await client.aclose()
    assert out == [_vec(7)]
    assert attempts["n"] == 2  # one failure, one success
