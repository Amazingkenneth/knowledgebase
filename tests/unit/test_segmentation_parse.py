"""JSON parsing / salvage / sanitizer behavior in segmentation.

Covers the failure modes observed against the real LLM:
  - Raw control chars (TAB/LF/CR) inside string values must be escaped, not
    preserved — this was the dominant root cause of `Unrecoverable JSON: char 0`.
  - The balanced-object sweep recovers entries from responses where the
    outer-array salvage gives up.
  - Markdown fences / leading prose are still tolerated.
"""
from __future__ import annotations

import pytest

from kb.services.segmentation import (
    _parse_json_array,
    _sanitize_json,
    _sweep_json_objects,
)


def test_sanitize_escapes_raw_tab_inside_string():
    raw = '[{"x": "a\tb"}]'  # raw TAB inside the string
    cleaned = _sanitize_json(raw)
    assert "\t" not in cleaned, "raw tab must be escaped or stripped"
    import json
    parsed = json.loads(cleaned)
    assert parsed[0]["x"] == "a\tb"


def test_sanitize_escapes_raw_newline_inside_string():
    raw = '[{"x": "line1\nline2"}]'
    import json
    parsed = json.loads(_sanitize_json(raw))
    assert parsed[0]["x"] == "line1\nline2"


def test_sanitize_preserves_outside_string_whitespace():
    raw = '[\n  {"x": "ok"},\n  {"x": "also ok"}\n]'
    import json
    parsed = json.loads(_sanitize_json(raw))
    assert len(parsed) == 2


def test_parse_recovers_response_with_raw_tabs():
    """Mimics the actual Qwen-turbo response that broke the production pipeline."""
    raw = (
        '[\n'
        '  {\n'
        '    "error_code": "300411",\n'
        '    "content": "Parameters:\t%1 = NC axis\n%2 = Drive number",\n'
        '    "resolution": "1. Control the SDB\n2. Replace",\n'
        '    "confidence": 0.9\n'
        '  }\n'
        ']'
    )
    entries = _parse_json_array(raw)
    assert len(entries) == 1
    assert entries[0]["error_code"] == "300411"
    # Content/resolution should be preserved (tab/newline round-trip).
    assert "Parameters:" in entries[0]["content"]


def test_sweep_recovers_objects_from_prose_soup():
    """When the outer-array salvage fails, the balanced-object sweep finds
    every parseable `{...}` block in the text."""
    raw = (
        'Sure, here you go:\n'
        '[ This is not really JSON\n'
        '{"a": 1, "b": "x"}\n'
        'random text inbetween\n'
        '{"a": 2, "nested": {"k": "v"}}\n'
        'more prose'
    )
    objs = _sweep_json_objects(raw)
    assert len(objs) == 2
    assert objs[0]["a"] == 1
    assert objs[1]["nested"]["k"] == "v"


def test_parse_tolerates_markdown_fence():
    raw = '```json\n[{"x": 1}]\n```'
    entries = _parse_json_array(raw)
    assert entries == [{"x": 1}]


def test_parse_tolerates_leading_prose():
    raw = 'Here is the JSON you asked for:\n[{"x": 1}]'
    entries = _parse_json_array(raw)
    assert entries == [{"x": 1}]


def test_parse_returns_single_object_as_list():
    """Some LLMs emit a bare object instead of a single-element array."""
    raw = '{"x": 1, "y": 2}'
    entries = _parse_json_array(raw)
    assert entries == [{"x": 1, "y": 2}]


def test_parse_empty_array():
    assert _parse_json_array("[]") == []


def test_parse_unrecoverable_raises():
    import json
    with pytest.raises(json.JSONDecodeError):
        _parse_json_array("not json at all, no brackets here either")
