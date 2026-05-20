from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class KnowledgeType(StrEnum):
    ALARM = "alarm"
    SETUP = "setup"
    EXPERIENCE = "experience"


class Taxonomy(BaseModel):
    """Runtime registry of valid filterable enums.

    Loaded from config/taxonomy.yaml. Acts as the single source of truth for
    indexing-time validation (reject unknown projects) and as Facets API output
    (so the upstream LLM knows MHK is a project, FTU is equipment).
    """

    version: str
    knowledge_types: list[KnowledgeType]
    projects: list[str] = Field(min_length=1)
    equipment: list[str] = Field(min_length=1)

    @field_validator("projects", "equipment")
    @classmethod
    def _no_blanks(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item or not item.strip():
                raise ValueError("taxonomy entries cannot be blank")
            if item != item.strip():
                raise ValueError(f"taxonomy entry has surrounding whitespace: {item!r}")
        if len(set(v)) != len(v):
            raise ValueError("taxonomy entries must be unique")
        return v

    def has_project(self, p: str) -> bool:
        return p in self.projects

    def has_equipment(self, e: str) -> bool:
        return e in self.equipment
