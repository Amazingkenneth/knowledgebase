"""Per-entry Raw Text excerpts must match the entry shown on the card.

A single chunk routinely yields many entries (e.g. every row of an alarm
table). The whole document arrives as one chunk for DOCX, and tabular DOCX is
rendered as a pipe-grid (one code per row). The excerpt attached to each
StagedDocument must be the slice of source text that actually backs *that*
entry — not a chunk-wide head that shows the same leading rows on every card.
"""
from __future__ import annotations

from kb.models.taxonomy import KnowledgeType
from kb.services.segmentation import _build_raw_excerpt, _parsed_to_staged

# A DOCX alarm table after extraction: each alarm code lives on its own
# pipe-grid row. Earlier rows pad the chunk past the 500-char excerpt window so
# the old "first 500 chars" behavior could never surface the later codes.
DOCX_TABLE = "\n".join(
    f"| E{100 + i:03d} | 报警标题 {i} | "
    f"原因描述 padding text to push later rows beyond the excerpt window {i} | "
    f"解除流程 step {i} |"
    for i in range(20)
)


def test_excerpt_anchors_on_error_code_not_chunk_head():
    entry = {"error_code": "E115", "title_zh": "报警标题 15"}
    excerpt = _build_raw_excerpt(entry, KnowledgeType.ALARM, DOCX_TABLE)
    assert "E115" in excerpt, "excerpt must contain the entry's own code"
    # And not bleed in from the unrelated first row.
    assert not excerpt.startswith("| E100 |")


def test_excerpt_starts_at_the_row_holding_the_code():
    entry = {"error_code": "E107"}
    excerpt = _build_raw_excerpt(entry, KnowledgeType.ALARM, DOCX_TABLE)
    assert excerpt.startswith("| E107 |"), "excerpt should begin at the code's row"


def test_each_card_gets_a_distinct_matching_excerpt():
    # Two entries from the SAME chunk must not share an identical head excerpt.
    e1 = {"error_code": "E101"}
    e2 = {"error_code": "E118"}
    x1 = _build_raw_excerpt(e1, KnowledgeType.ALARM, DOCX_TABLE)
    x2 = _build_raw_excerpt(e2, KnowledgeType.ALARM, DOCX_TABLE)
    assert x1 != x2
    assert "E101" in x1 and "E118" in x2


def test_excerpt_preserves_table_layout():
    entry = {"error_code": "E103"}
    excerpt = _build_raw_excerpt(entry, KnowledgeType.ALARM, DOCX_TABLE)
    # Pipe-grid structure survives (not whitespace-collapsed) so <pre> renders
    # the row as a table.
    assert "|" in excerpt


def test_excerpt_falls_back_to_head_when_anchor_absent():
    entry = {"error_code": "Z999"}  # not present in the text
    excerpt = _build_raw_excerpt(entry, KnowledgeType.ALARM, DOCX_TABLE)
    assert excerpt.startswith("| E100 |")


def test_experience_anchors_on_problem():
    raw = (
        "无关前言占位文字，把真正的条目推到后面去。\n"
        "问题：晶圆对准失败，传感器读数漂移。\n"
        "失败分析：光源老化导致信噪比下降。"
    )
    entry = {"problem": "晶圆对准失败，传感器读数漂移", "failure_desc": "光源老化"}
    excerpt = _build_raw_excerpt(entry, KnowledgeType.EXPERIENCE, raw)
    assert "晶圆对准失败" in excerpt
    assert not excerpt.startswith("无关前言")


def test_parsed_to_staged_excerpt_matches_code():
    # End-to-end through the staging conversion: the card's raw_text_excerpt
    # must mention the same code the card displays.
    entry = {
        "error_code": "E112",
        "title_zh": "标题",
        "content": "原因描述 padding text to push later rows beyond the excerpt window 12",
        "resolution": "解除流程 step 12",
        "confidence": 0.9,
    }
    doc = _parsed_to_staged(
        0, entry, KnowledgeType.ALARM, "alarms.docx", None, None,
        raw_chunk_text=DOCX_TABLE, normalized_full_raw=" ".join(DOCX_TABLE.split()),
    )
    assert doc.error_codes == ["E112"]
    assert "E112" in doc.raw_text_excerpt
