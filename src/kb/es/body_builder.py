"""Deterministic construction of the searchable `body` field.

Per thoughts.md, the `body` text must contain:
  1. All long-form content (内容 / 解除流程 / 注意事项 etc.) joined with a
     visible separator so analyzer boundaries are respected.
  2. The Part-1 metadata fields (project, equipment, error_codes) and title,
     appended one per line — so a keyword query for "MHK" recalls docs where
     MHK lives only in the `project` field.

The format is pinned by tests; do not change it without bumping the index
version and reindexing.
"""

from __future__ import annotations

from kb.models.document import DocumentBase

CONTENT_SEPARATOR = "\n\n---\n\n"
META_SEPARATOR = "\n"


def build_body(doc: DocumentBase) -> str:
    """Build the `body` text indexed into ES.

    Layout:
        <section_1_text>
        \\n\\n---\\n\\n
        <section_2_text>
        ...
        \\n\\n---\\n\\n
        <title>
        \\n
        project: <project>
        \\n
        equipment: <equipment>
        \\n
        error_codes: <code1> <code2> ...
    """
    parts: list[str] = [text for _, text in doc.content_sections()]

    # Metadata block — title first, then key:value lines. Keep keys lowercase
    # and stable: tokenization should split "project:" and "MHK" so a query
    # for "MHK" still matches.
    meta_lines: list[str] = [
        doc.title,
        f"project: {doc.project}",
        f"equipment: {doc.equipment}",
    ]
    if doc.error_codes:
        meta_lines.append("error_codes: " + " ".join(doc.error_codes))

    metadata_block = META_SEPARATOR.join(meta_lines)
    parts.append(metadata_block)

    return CONTENT_SEPARATOR.join(parts)


def build_title_text(doc: DocumentBase) -> str:
    """The `title` field is the bare doc title — no concatenation.

    Helper exists for symmetry with build_body and to give a single seam if we
    ever decide to prepend project/equipment to the indexed title.
    """
    return doc.title
