"""Settings schema: env-var precedence, nested delimiter, bounds validation,
and presence of the robustness knobs added for extraction/session limits.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kb.config import Settings


def test_env_var_overrides_with_nested_delimiter(monkeypatch):
    monkeypatch.setenv("KB_ES__URL", "http://override:9200")
    monkeypatch.setenv("KB_SEARCH__STRICT_MAX_HITS", "5")
    s = Settings()
    assert s.es.url == "http://override:9200"
    assert s.search.strict_max_hits == 5


def test_out_of_range_value_is_rejected(monkeypatch):
    # strict_max_hits is bounded to [1, 50].
    monkeypatch.setenv("KB_SEARCH__STRICT_MAX_HITS", "999")
    with pytest.raises(ValidationError):
        Settings()


def test_new_limit_knobs_have_defaults():
    s = Settings()
    assert s.ingest.pdf_max_pages >= 1
    assert s.ingest.xlsx_max_cells >= 1000
    assert s.ingest.session_evict_interval_minutes >= 1


def test_session_evict_interval_is_bounded(monkeypatch):
    monkeypatch.setenv("KB_INGEST__SESSION_EVICT_INTERVAL_MINUTES", "0")
    with pytest.raises(ValidationError):
        Settings()
