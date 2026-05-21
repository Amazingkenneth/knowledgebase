"""Elasticsearch index mappings.

One index alias per knowledge_type (kb_alarm, kb_setup, kb_experience).
Physical indices are versioned (kb_alarm_v1) so we can reindex without downtime.

`dynamic: "strict"` on every mapping — unknown fields are rejected at index
time. This is a deliberate validation choice (see plan, Section: Validation).

Analyzer choice:
  - With IK plugin: use "ik_max_word" (index) + "ik_smart" (query). Best for Chinese.
    Install: bin/elasticsearch-plugin install analysis-ik
    Then set es.analyzer_index="ik_max_word" and es.analyzer_query="ik_smart" in settings.
  - Without IK (default): falls back to "cjk" which does CJK bigram analysis —
    built into every ES distribution, no plugin needed. Adequate for Chinese queries.
"""

from __future__ import annotations

from typing import Any

from kb.models.taxonomy import KnowledgeType


def _analyzer_settings(index_analyzer: str, query_analyzer: str) -> dict[str, Any] | None:
    """Return custom analyzer settings only if IK (custom tokenizers) is requested.

    For built-in analyzers (cjk, standard, etc.) no settings block is needed.
    """
    ik_tokenizers = {"ik_max_word", "ik_smart"}
    if index_analyzer in ik_tokenizers or query_analyzer in ik_tokenizers:
        return {
            "analysis": {
                "analyzer": {
                    "kb_index": {"type": "custom", "tokenizer": index_analyzer},
                    "kb_query": {"type": "custom", "tokenizer": query_analyzer},
                }
            }
        }
    return None


def _base_mapping(dims: int, index_analyzer: str, query_analyzer: str) -> dict[str, Any]:
    return {
        "dynamic": "strict",
        "properties": {
            # Part 1 — keyword fields: participate in filter (bool/term) and
            #   exact-string matching; NOT tokenised; do NOT affect BM25 scoring.
            "knowledge_type": {"type": "keyword"},
            "project": {"type": "keyword"},
            "equipment": {"type": "keyword"},
            "error_codes": {"type": "keyword"},
            # Part 2 — display-only: stored but NOT indexed (index: False / enabled: False).
            #   Retrieved verbatim for rendering; never used in queries or scoring.
            "source_file": {"type": "keyword", "index": False},
            "source_pages": {"type": "keyword", "index": False},
            "sections": {"type": "object", "enabled": False},
            # summary: ≤50-char human digest, display-only, prevents context overflow.
            "summary": {"type": "keyword", "index": False},
            # Part 3 — full-text fields: tokenised with the configured analyzer;
            #   participate in BM25 keyword recall AND vector rescoring.
            "title": {
                "type": "text",
                "analyzer": index_analyzer,
                "search_analyzer": query_analyzer,
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
            },
            "body": {
                "type": "text",
                "analyzer": index_analyzer,
                "search_analyzer": query_analyzer,
            },
            "title_vec": {
                "type": "dense_vector",
                "dims": dims,
                "index": True,
                "similarity": "cosine",
                "index_options": {"type": "hnsw"},
            },
            "body_vec": {
                "type": "dense_vector",
                "dims": dims,
                "index": True,
                "similarity": "cosine",
                "index_options": {"type": "hnsw"},
            },
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        },
    }


def index_body(dims: int, index_analyzer: str = "cjk", query_analyzer: str = "cjk") -> dict[str, Any]:
    """Full create-index body (settings + mappings).

    Same shape for every knowledge_type — differentiation is at the application
    layer (which content sections go into `body`). Sharing one mapping keeps
    cross-type search trivial.

    Default analyzer is `cjk` (built-in CJK bigram). To use IK for better
    Chinese tokenization, install the analysis-ik plugin and pass:
        index_analyzer="ik_max_word", query_analyzer="ik_smart"
    """
    body: dict[str, Any] = {
        "mappings": _base_mapping(dims, index_analyzer, query_analyzer),
    }
    ana_settings = _analyzer_settings(index_analyzer, query_analyzer)
    if ana_settings:
        body["settings"] = ana_settings
    return body


def index_name(prefix: str, kt: KnowledgeType, version: int = 1) -> str:
    return f"{prefix}_{kt.value}_v{version}"


def alias_name(prefix: str, kt: KnowledgeType) -> str:
    return f"{prefix}_{kt.value}"


def all_alias_pattern(prefix: str) -> str:
    return f"{prefix}_*"
