"""Request-size guards.

Oversized inputs must be rejected at validation time (HTTP 400 via the
project's RequestValidationError handler) rather than flowing into an ES query
or the LLM context where they amplify memory / token cost.
"""

from __future__ import annotations

from kb.api.chat import _MAX_MESSAGE_CHARS, _MAX_MESSAGES
from kb.models.search import SearchRequest


def test_search_rejects_too_many_keywords(make_client):
    with make_client() as c:
        r = c.post("/api/v1/search", json={"keywords": ["k"] * 65})
    assert r.status_code == 400


def test_search_rejects_overlong_query_text(make_client):
    with make_client() as c:
        r = c.post("/api/v1/search", json={"query_text": "x" * 4001})
    assert r.status_code == 400


def test_search_accepts_at_the_limit():
    # Model-level: exactly at the bounds is valid.
    req = SearchRequest(keywords=["k"] * 64, query_text="x" * 4000, error_codes=["e"] * 64)
    assert len(req.keywords) == 64


def test_chat_rejects_empty_message_list(make_client):
    with make_client() as c:
        r = c.post("/api/v1/chat", json={"messages": []})
    assert r.status_code == 400


def test_chat_rejects_too_many_messages(make_client):
    msgs = [{"role": "user", "content": "hi"}] * (_MAX_MESSAGES + 1)
    with make_client() as c:
        r = c.post("/api/v1/chat", json={"messages": msgs})
    assert r.status_code == 400


def test_chat_rejects_overlong_message_content(make_client):
    msgs = [{"role": "user", "content": "x" * (_MAX_MESSAGE_CHARS + 1)}]
    with make_client() as c:
        r = c.post("/api/v1/chat", json={"messages": msgs})
    assert r.status_code == 400


def test_extract_rejects_overlong_query(make_client):
    with make_client() as c:
        r = c.post("/api/v1/extract", json={"query": "x" * (_MAX_MESSAGE_CHARS + 1)})
    assert r.status_code == 400


def test_extract_rejects_empty_query(make_client):
    with make_client() as c:
        r = c.post("/api/v1/extract", json={"query": ""})
    assert r.status_code == 400
