"""Pin the ES query DSL we build.

These tests don't talk to a live ES; they assert the shape of the dict we
pass to es.search() so we catch accidental query regressions cheaply.
"""

from kb.models.search import SearchRequest
from kb.models.taxonomy import KnowledgeType
from kb.services.search import _bm25_query, _filters, _kw_multi_match


def test_filters_includes_all_part1():
    req = SearchRequest(
        knowledge_type=KnowledgeType.ALARM,
        project="FTU",
        equipment="Sphere",
        error_codes=["125002", "124000"],
    )
    f = _filters(req)
    assert {"term": {"project": "FTU"}} in f
    assert {"term": {"equipment": "Sphere"}} in f
    assert {"terms": {"error_codes": ["125002", "124000"]}} in f


def test_filters_empty_when_no_params():
    req = SearchRequest(knowledge_type=KnowledgeType.ALARM)
    assert _filters(req) == []


def test_strict_query_uses_and_operator():
    req = SearchRequest(
        knowledge_type=KnowledgeType.ALARM,
        keywords=["MHK", "穿梭"],
        project="MHK",
    )
    q = _bm25_query(req, "and", title_boost=3.0)
    must = q["bool"]["must"]
    assert must[0]["multi_match"]["operator"] == "and"
    assert must[0]["multi_match"]["query"] == "MHK 穿梭"


def test_loose_query_uses_or_operator():
    req = SearchRequest(knowledge_type=KnowledgeType.ALARM, keywords=["a", "b"])
    q = _bm25_query(req, "or", title_boost=3.0)
    assert q["bool"]["must"][0]["multi_match"]["operator"] == "or"


def test_match_all_when_no_keywords():
    req = SearchRequest(knowledge_type=KnowledgeType.ALARM, project="FTU")
    q = _bm25_query(req, "and", title_boost=3.0)
    assert q["bool"]["must"] == [{"match_all": {}}]


def test_title_boost_applied():
    clause = _kw_multi_match(["x"], "and", title_boost=5.0)
    assert "title^5.0" in clause["multi_match"]["fields"]
    assert "body" in clause["multi_match"]["fields"]
