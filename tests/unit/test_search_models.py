import pytest
from pydantic import ValidationError

from kb.models.search import SearchRequest, SearchStatus
from kb.models.taxonomy import KnowledgeType


def test_search_request_minimal():
    req = SearchRequest(knowledge_type=KnowledgeType.ALARM)
    assert req.mode == "auto"
    assert req.size == 10
    assert req.keywords == []
    assert req.error_codes == []


def test_search_request_size_bounds():
    with pytest.raises(ValidationError):
        SearchRequest(knowledge_type=KnowledgeType.ALARM, size=0)
    with pytest.raises(ValidationError):
        SearchRequest(knowledge_type=KnowledgeType.ALARM, size=999)


def test_search_status_values():
    """Pin the SearchStatus contract — callers depend on these exact strings."""
    assert SearchStatus.STRICT_HIT.value == "strict_hit"
    assert SearchStatus.TOO_MANY.value == "too_many"
    assert SearchStatus.LOOSE_HIT.value == "loose_hit"
    assert SearchStatus.VECTOR_ONLY.value == "vector_only"
    assert SearchStatus.NO_HIT.value == "no_hit"


def test_search_request_mode_enum():
    with pytest.raises(ValidationError):
        SearchRequest(knowledge_type=KnowledgeType.ALARM, mode="random")  # type: ignore[arg-type]
