from kb.es.body_builder import build_body, build_title_text
from kb.models.document import AlarmDoc


def _alarm(**overrides) -> AlarmDoc:
    base = dict(
        project="FTU",
        equipment="Sphere",
        error_codes=["125002", "124000"],
        title="穿梭真空感应失败",
        content="1.穿梭真空报警分为两种…………",
        resolution="1.确认对应报警穿梭穴位…………",
        notes="检修时注意安全。",
    )
    base.update(overrides)
    return AlarmDoc(**base)


def test_body_contains_all_content_sections():
    doc = _alarm()
    body = build_body(doc)
    assert doc.content in body
    assert doc.resolution in body
    assert doc.notes in body


def test_body_appends_metadata_for_recall():
    """A query for "MHK" must recall a doc where MHK is only in `project`."""
    doc = _alarm(project="MHK")
    body = build_body(doc)
    # The metadata block ensures the project token lives inside `body`.
    assert "MHK" in body
    assert "project: MHK" in body
    assert "equipment: Sphere" in body
    assert "error_codes:" in body
    assert "125002" in body and "124000" in body


def test_body_omits_blank_optional_sections():
    doc = _alarm(notes="")
    body = build_body(doc)
    assert "1.穿梭真空报警" in body
    # Notes blank => not in the concatenation. Confirms section_sections() filtering.
    assert "检修时注意安全" not in body


def test_body_format_pinned():
    """If this test breaks, you must bump the index version and reindex.

    The exact layout is part of the cross-system contract.
    """
    doc = _alarm()
    body = build_body(doc)
    expected_tail = (
        "\n\n---\n\n"
        "穿梭真空感应失败\n"
        "project: FTU\n"
        "equipment: Sphere\n"
        "error_codes: 125002 124000"
    )
    assert body.endswith(expected_tail), f"body ended with: {body[-200:]!r}"


def test_title_text_is_just_the_title():
    doc = _alarm()
    assert build_title_text(doc) == "穿梭真空感应失败"


def test_body_no_metadata_block_without_error_codes():
    doc = _alarm(error_codes=[])
    body = build_body(doc)
    assert "error_codes" not in body
    assert "project: FTU" in body
