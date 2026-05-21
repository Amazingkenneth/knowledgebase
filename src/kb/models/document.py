from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from kb.models.taxonomy import KnowledgeType

ERROR_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_\-]{0,63}$")

NonEmptyStr = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
TitleStr = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
SummaryStr = Annotated[str, StringConstraints(max_length=50, strip_whitespace=True)]


class DocumentBase(BaseModel):
    """Fields common to every knowledge document.

    Subclass per knowledge_type to add structured content fields. Indexing
    builds the ES `body` text by concatenating subclass-defined content
    sections + Part-1 metadata (see es/body_builder.py).
    """

    knowledge_type: KnowledgeType
    project: NonEmptyStr
    equipment: NonEmptyStr
    error_codes: list[str] = Field(default_factory=list)
    title: TitleStr

    # Part 2 — display only (index: False in ES; never used in queries or scoring)
    source_file: str | None = None
    source_pages: list[str] = Field(default_factory=list)
    # ≤50-char digest shown in result lists; avoids sending full sections to the LLM context.
    summary: SummaryStr | None = None

    # Audit
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("error_codes")
    @classmethod
    def _validate_codes(cls, codes: list[str]) -> list[str]:
        out: list[str] = []
        for raw in codes:
            c = raw.strip().upper()
            if not ERROR_CODE_RE.match(c):
                raise ValueError(
                    f"invalid error_code {raw!r}: must match {ERROR_CODE_RE.pattern}"
                )
            out.append(c)
        if len(set(out)) != len(out):
            raise ValueError("error_codes must be unique")
        return out

    def content_sections(self) -> list[tuple[str, str]]:
        """Override per subclass. Returns ordered (section_name, text) pairs.

        body_builder concatenates these with a stable separator. Section names
        are NOT shown in the body — they are for the body_builder's own use
        (e.g., to skip empty sections).
        """
        raise NotImplementedError


class AlarmDoc(DocumentBase):
    knowledge_type: Literal[KnowledgeType.ALARM] = KnowledgeType.ALARM

    # 内容 / 解除流程 / 注意事项
    content: NonEmptyStr
    resolution: NonEmptyStr
    notes: str = ""

    def content_sections(self) -> list[tuple[str, str]]:
        sections = [("content", self.content), ("resolution", self.resolution)]
        if self.notes.strip():
            sections.append(("notes", self.notes))
        return sections


class SetupDoc(DocumentBase):
    knowledge_type: Literal[KnowledgeType.SETUP] = KnowledgeType.SETUP

    procedure: NonEmptyStr
    prerequisites: str = ""
    notes: str = ""

    def content_sections(self) -> list[tuple[str, str]]:
        sections: list[tuple[str, str]] = []
        if self.prerequisites.strip():
            sections.append(("prerequisites", self.prerequisites))
        sections.append(("procedure", self.procedure))
        if self.notes.strip():
            sections.append(("notes", self.notes))
        return sections


class ExperienceDoc(DocumentBase):
    knowledge_type: Literal[KnowledgeType.EXPERIENCE] = KnowledgeType.EXPERIENCE

    body_text: NonEmptyStr
    procedure: str = ""
    notes: str = ""

    def content_sections(self) -> list[tuple[str, str]]:
        sections: list[tuple[str, str]] = [("body", self.body_text)]
        if self.procedure.strip():
            sections.append(("procedure", self.procedure))
        if self.notes.strip():
            sections.append(("notes", self.notes))
        return sections


# Discriminated union for API ingress.
KnowledgeDoc = AlarmDoc | SetupDoc | ExperienceDoc


def doc_class_for(kt: KnowledgeType) -> type[DocumentBase]:
    match kt:
        case KnowledgeType.ALARM:
            return AlarmDoc
        case KnowledgeType.SETUP:
            return SetupDoc
        case KnowledgeType.EXPERIENCE:
            return ExperienceDoc
