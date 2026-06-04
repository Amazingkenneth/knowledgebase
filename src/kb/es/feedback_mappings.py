"""Elasticsearch index mapping for search relevance feedback (kb_search_feedback).

Stores lightweight 👍/👎 signals on individual search results so operators can
spot low-quality queries and tune ranking (title_boost / vector_weight /
rrf_window). It never feeds back into search itself — purely observational.
"""

from __future__ import annotations

from typing import Any

FEEDBACK_INDEX_NAME = "kb_search_feedback"

FEEDBACK_INDEX_BODY: dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "doc_id": {"type": "keyword"},
            "helpful": {"type": "boolean"},
            "query_text": {"type": "keyword"},
            "knowledge_type": {"type": "keyword"},
            "project": {"type": "keyword"},
            "equipment": {"type": "keyword"},
            "search_status": {"type": "keyword"},
            "request_id": {"type": "keyword"},
            "created_at": {"type": "date"},
        },
    },
}
