from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from kb.models.taxonomy import KnowledgeType


class SearchStatus(StrEnum):
    """Cross-system contract telling the caller how to render results.

    Encodes the thoughts.md rule that loose hits MUST be marked as "for
    reference only" — putting this in the type system means a UI cannot
    accidentally present them as authoritative.
    """

    STRICT_HIT = "strict_hit"        # All filters + AND-of-keywords matched within threshold.
    TOO_MANY = "too_many"            # Strict matched > strict_max_hits; ask user to narrow.
    LOOSE_HIT = "loose_hit"          # Fell back to OR-of-keywords; render with "仅供参考" banner.
    VECTOR_ONLY = "vector_only"      # Only vector similarity matched (low confidence).
    NO_HIT = "no_hit"                # Nothing matched, even with vector.


SearchMode = Literal["auto", "strict", "loose", "vector_only"]


class SearchRequest(BaseModel):
    """Already-extracted, structured search parameters.

    The conversational layer (out of scope here) is responsible for parsing
    natural language and populating this. We never accept raw NL queries that
    bypass the filter/keyword/vector split.

    `knowledge_type` is optional — when None the search spans all indices.
    """

    knowledge_type: KnowledgeType | None = None
    project: str | None = None
    equipment: str | None = None
    error_codes: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    query_text: str | None = None
    mode: SearchMode = "auto"
    size: int = Field(default=10, ge=1, le=50)
    from_: int = Field(default=0, ge=0)


class DocHit(BaseModel):
    id: str
    score: float
    knowledge_type: KnowledgeType
    project: str
    equipment: str
    error_codes: list[str]
    title: str
    source_file: str | None = None
    source_pages: list[str] = Field(default_factory=list)
    # ≤50-char digest for result list display; avoids sending full sections to LLM context.
    summary: str | None = None
    # Original content sections from the source doc — verbatim, never AI-rewritten.
    sections: dict[str, str] = Field(default_factory=dict)


class EffectiveParams(BaseModel):
    """Round-trip echo of what was actually applied, after normalization.

    The upstream chat layer uses this to render "您询问 MEM 项目、Sphere 机台…"
    so the user can catch misextraction immediately. Per thoughts.md.
    """

    knowledge_type: KnowledgeType | None = None
    project: str | None = None
    equipment: str | None = None
    error_codes: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    status: SearchStatus
    total: int
    hits: list[DocHit]
    effective_params: EffectiveParams
    # Populated only when status == TOO_MANY — facet counts to help the caller
    # decide what to ask the user to narrow on.
    facets: dict[str, dict[str, int]] = Field(default_factory=dict)
    # Human-readable banner the caller MUST render verbatim (for LOOSE/VECTOR_ONLY/NO_HIT).
    banner: str | None = None
