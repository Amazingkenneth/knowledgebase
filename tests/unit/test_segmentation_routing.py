"""Per-chunk routing + skip behavior in segment_text.

Stubs the LLM at the HTTP layer so no network calls happen. Verifies:
  - Mixed-type files route different chunks to different parsers.
  - "skip" classifier verdict drops the chunk and surfaces a SkippedChunk.
  - knowledge_type lock bypasses the classifier entirely.
  - No-entry chunks surface a friendly hint.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from kb.config import Settings
from kb.models.taxonomy import KnowledgeType
from kb.services.segmentation import segment_text


def _settings() -> Settings:
    s = Settings()
    s.llm.api_key = "fake-test-key"
    return s


def _mk_response(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}]}


@pytest.mark.asyncio
async def test_skip_chunks_drop_and_report():
    """Classifier returns ['skip'] → chunk is not parsed; SkippedChunk surfaced."""
    pages = [(1, "Table of Contents\n  1. Intro ........ 1\n  2. Alarms ....... 5")]

    async def fake_post(self, url, **kwargs):  # noqa: ARG001
        payload = kwargs.get("json", {})
        msgs = payload.get("messages", [])
        sys_msg = msgs[0]["content"] if msgs else ""
        if "Classify" in sys_msg:
            body = _mk_response(json.dumps({"types": ["skip"]}))
        else:
            body = _mk_response("[]")
        resp = type("R", (), {})()
        resp.status_code = 200
        resp.json = lambda: body
        resp.text = ""
        return resp

    with patch("httpx.AsyncClient.post", new=fake_post):
        docs, skipped = await segment_text(
            _settings(), pages, knowledge_type=None, file_name="toc.pdf",
        )
    assert docs == []
    assert len(skipped) == 1
    assert skipped[0].reason == "non_content"
    assert "cover" in skipped[0].hint or "content" in skipped[0].hint.lower()


@pytest.mark.asyncio
async def test_locked_type_bypasses_classifier():
    """When knowledge_type is set, the classifier is not invoked."""
    pages = [(1, "1030 Cycle time exceeded. Remedy: check program.")]
    classify_calls = 0
    seg_calls = 0

    async def fake_post(self, url, **kwargs):  # noqa: ARG001
        nonlocal classify_calls, seg_calls
        msgs = kwargs.get("json", {}).get("messages", [])
        sys_msg = msgs[0]["content"] if msgs else ""
        if "Classify" in sys_msg:
            classify_calls += 1
            body = _mk_response(json.dumps({"types": ["alarm"]}))
        else:
            seg_calls += 1
            body = _mk_response(json.dumps([{
                "error_code": "1030", "title": "Cycle time exceeded",
                "content": "—", "resolution": "check program",
                "source_pages": [1], "confidence": 0.9,
            }]))
        resp = type("R", (), {})()
        resp.status_code = 200
        resp.json = lambda: body
        resp.text = ""
        return resp

    with patch("httpx.AsyncClient.post", new=fake_post):
        docs, skipped = await segment_text(
            _settings(), pages, knowledge_type=KnowledgeType.ALARM,
            file_name="alarms.pdf",
        )
    assert classify_calls == 0, "locked type should skip the classifier"
    assert seg_calls == 1
    assert len(docs) == 1
    assert docs[0].knowledge_type == KnowledgeType.ALARM
    assert skipped == []


@pytest.mark.asyncio
async def test_classify_chunk_types_skip_and_route():
    """classify_chunk_types returns [] for skip and the right enum list otherwise."""
    from kb.services.segmentation import classify_chunk_types

    cases = [
        (["skip"], []),
        (["alarm"], [KnowledgeType.ALARM]),
        (["setup"], [KnowledgeType.SETUP]),
        (["experience"], [KnowledgeType.EXPERIENCE]),
        (["alarm", "setup"], [KnowledgeType.ALARM, KnowledgeType.SETUP]),
    ]
    for types_in, expected in cases:
        async def fake_post(self, url, _t=types_in, **kwargs):  # noqa: ARG001
            resp = type("R", (), {})()
            resp.status_code = 200
            resp.json = lambda: _mk_response(json.dumps({"types": _t}))
            resp.text = ""
            return resp

        with patch("httpx.AsyncClient.post", new=fake_post):
            got = await classify_chunk_types(_settings(), "any text")
        assert got == expected, f"{types_in} → expected {expected}, got {got}"


@pytest.mark.asyncio
async def test_multi_type_chunk_routed_to_each_type():
    """A single chunk classified as alarm+setup should be parsed by BOTH parsers."""
    pages = [(1, "Alarm E1001 vacuum loss... Setup: 1. install plate. 2. calibrate.")]

    async def fake_post(self, url, **kwargs):  # noqa: ARG001
        msgs = kwargs.get("json", {}).get("messages", [])
        sys_msg = msgs[0]["content"] if msgs else ""
        if "Classify" in sys_msg:
            body = _mk_response(json.dumps({"types": ["alarm", "setup"]}))
        elif "alarm" in sys_msg.lower() and "parser" in sys_msg.lower():
            body = _mk_response(json.dumps([{
                "error_code": "E1001", "title_zh": "真空丢失", "title_en": "Vacuum Loss",
                "content": "vacuum loss", "resolution": "—",
                "source_pages": [1], "confidence": 0.9,
            }]))
        else:
            body = _mk_response(json.dumps([{
                "station": "Plate calibration",
                "procedure": "1. install plate. 2. calibrate.",
                "source_pages": [1], "confidence": 0.85,
            }]))
        resp = type("R", (), {})()
        resp.status_code = 200
        resp.json = lambda: body
        resp.text = ""
        return resp

    with patch("httpx.AsyncClient.post", new=fake_post):
        docs, skipped = await segment_text(
            _settings(), pages, knowledge_type=None, file_name="mixed.pdf",
        )
    types = {d.knowledge_type for d in docs}
    assert KnowledgeType.ALARM in types
    assert KnowledgeType.SETUP in types
    # No no_entries hint should fire — both parsers produced output.
    assert not any(s.reason == "no_entries" for s in skipped)


@pytest.mark.asyncio
async def test_no_entries_surfaces_friendly_hint():
    """Classifier picks a type but the segmenter finds nothing → hint."""
    pages = [(1, "Some prose that doesn't contain any concrete alarm entries.")]

    async def fake_post(self, url, **kwargs):  # noqa: ARG001
        msgs = kwargs.get("json", {}).get("messages", [])
        sys_msg = msgs[0]["content"] if msgs else ""
        body = _mk_response(
            json.dumps({"types": ["alarm"]}) if "Classify" in sys_msg else "[]"
        )
        resp = type("R", (), {})()
        resp.status_code = 200
        resp.json = lambda: body
        resp.text = ""
        return resp

    with patch("httpx.AsyncClient.post", new=fake_post):
        docs, skipped = await segment_text(
            _settings(), pages, knowledge_type=None, file_name="prose.pdf",
        )
    assert docs == []
    assert any(s.reason == "no_entries" for s in skipped)
    assert any("alarm" in s.hint for s in skipped)
