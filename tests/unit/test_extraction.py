"""Unit tests for file extraction and related utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from kb.models.taxonomy import KnowledgeType
from kb.services.extraction import (
    EXTRACTORS,
    _should_use_ocr,
    extract_csv,
    extract_file,
)
from kb.services.segmentation import (
    _group_duplicates,
    _parse_json_array,
    _split_oversized_page,
    chunk_pages,
    verify_extraction_fidelity,
)


class TestExtractCSV:
    def test_basic_csv(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "test.csv"
        csv_file.write_text("col1,col2\nval1,val2\nval3,val4", encoding="utf-8")
        pages = extract_csv(csv_file)
        assert len(pages) == 1
        assert pages[0][0] == 1
        assert "val1" in pages[0][1]
        assert "val3" in pages[0][1]

    def test_empty_csv(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "empty.csv"
        csv_file.write_text("", encoding="utf-8")
        pages = extract_csv(csv_file)
        assert pages == []

    def test_chinese_csv(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "chinese.csv"
        csv_file.write_text("项目,机台\nMHK,Loader", encoding="utf-8-sig")
        pages = extract_csv(csv_file)
        assert len(pages) == 1
        assert "MHK" in pages[0][1]


class TestExtractFile:
    def test_unsupported_type(self, tmp_path: Path) -> None:
        f = tmp_path / "test.xyz"
        f.write_text("content")
        with pytest.raises(ValueError, match="Unsupported file type"):
            extract_file(f)

    def test_csv_dispatch(self, tmp_path: Path) -> None:
        f = tmp_path / "test.csv"
        f.write_text("a,b\n1,2", encoding="utf-8")
        pages = extract_file(f, ocr_enabled=False)
        assert len(pages) == 1

    def test_supported_extensions(self) -> None:
        assert set(EXTRACTORS.keys()) == {"pdf", "pptx", "docx", "xlsx", "xls", "csv"}


class _FakePage:
    """Minimal stand-in for a pymupdf page for OCR-trigger tests."""

    def __init__(self, *, images: int = 0, coverage_bbox: tuple | None = None) -> None:
        self._images = images
        self._bbox = coverage_bbox

        class _Rect:
            width = 100.0
            height = 100.0

        self.rect = _Rect()

    def get_images(self, full: bool = False) -> list:
        return [object()] * self._images

    def get_image_info(self) -> list:
        return [{"bbox": self._bbox}] if self._bbox else []


class TestShouldUseOCR:
    def test_disabled_never_ocrs(self) -> None:
        page = _FakePage(images=1)
        assert _should_use_ocr("", page, ocr_enabled=False) is False

    def test_sparse_text_with_image_triggers(self) -> None:
        # Scanned page leaks a short header line but is image-backed.
        page = _FakePage(images=1)
        assert _should_use_ocr("Page 1 of 60", page, ocr_enabled=True) is True

    def test_image_dominated_page_triggers_even_with_text(self) -> None:
        # Half-page image with some leaked text → coverage rule kicks in.
        page = _FakePage(images=1, coverage_bbox=(0, 0, 100, 60))
        leaked = "x" * 200  # over the sparse-text threshold
        assert _should_use_ocr(leaked, page, ocr_enabled=True) is True

    def test_clean_text_no_images_skips(self) -> None:
        page = _FakePage(images=0)
        assert _should_use_ocr("plenty of real text " * 20, page, ocr_enabled=True) is False

    def test_garbled_printable_ratio_triggers(self) -> None:
        page = _FakePage(images=0)
        garbled = "\x01\x02\x03\x04\x05\x06" * 30
        assert _should_use_ocr(garbled, page, ocr_enabled=True) is True


class TestChunkPages:
    def test_empty(self) -> None:
        assert chunk_pages([]) == []

    def test_single_small_page(self) -> None:
        pages = [(1, "short text")]
        chunks = chunk_pages(pages, max_chars=100)
        assert len(chunks) == 1
        assert chunks[0] == pages

    def test_splits_large_content(self) -> None:
        pages = [(i, f"Page {i} " * 100) for i in range(1, 6)]
        chunks = chunk_pages(pages, max_chars=500)
        assert len(chunks) > 1

    def test_overlap_when_it_fits(self) -> None:
        # Pages small enough that the 1-page overlap still fits the budget →
        # the last page of chunk N is repeated as the first page of chunk N+1.
        pages = [(i, "x" * 100) for i in range(1, 4)]
        chunks = chunk_pages(pages, max_chars=250)
        assert len(chunks) > 1
        for i in range(len(chunks) - 1):
            assert chunks[i][-1] == chunks[i + 1][0]

    def test_chunks_never_exceed_budget(self) -> None:
        # Regression: a near-full page carried as overlap must not combine with
        # the next page into an over-budget chunk. Previously the overlap page
        # was re-added unconditionally, producing chunks well over max_chars
        # (e.g. 16k against a 12k budget) that the LLM could truncate — dropping
        # every entry on the page ("No documents extracted").
        max_chars = 1000
        pages = [
            (1, "a" * 900),
            (2, "b" * 900),
            (3, "c" * 500),
        ]
        chunks = chunk_pages(pages, max_chars=max_chars)
        for chunk in chunks:
            assert sum(len(text) for _, text in chunk) <= max_chars

    def test_no_split_needed(self) -> None:
        pages = [(1, "a"), (2, "b"), (3, "c")]
        chunks = chunk_pages(pages, max_chars=10000)
        assert len(chunks) == 1


class TestVerifyExtractionFidelity:
    def test_exact_match(self) -> None:
        assert verify_extraction_fidelity("hello world", "hello world")

    def test_substring_match(self) -> None:
        assert verify_extraction_fidelity("hello", "prefix hello suffix")

    def test_no_match(self) -> None:
        assert not verify_extraction_fidelity("fabricated", "real source text")

    def test_whitespace_normalization(self) -> None:
        assert verify_extraction_fidelity("hello  world", "prefix hello world suffix")

    def test_long_text_sentence_overlap(self) -> None:
        raw = "第一句话。第二句话。第三句话。第四句话。" * 20
        field = "第一句话。第二句话。第三句话。" * 15
        assert verify_extraction_fidelity(field, raw)


class TestGroupDuplicateAlarms:
    def test_no_duplicates_all_standalone(self) -> None:
        entries = [
            ({"error_code": "E1001", "confidence": 0.9}, "chunk1"),
            ({"error_code": "E1002", "confidence": 0.8}, "chunk2"),
        ]
        result = _group_duplicates(entries, KnowledgeType.ALARM, "f.pdf")
        assert len(result) == 2
        # Distinct codes ⇒ no group, both primary.
        assert all(gid is None and primary for _, _, gid, primary in result)

    def test_duplicates_grouped_not_dropped(self) -> None:
        entries = [
            ({"error_code": "E1001", "confidence": 0.7}, "chunk1"),
            ({"error_code": "E1001", "confidence": 0.95}, "chunk2"),
        ]
        result = _group_duplicates(entries, KnowledgeType.ALARM, "f.pdf")
        # Both variants are kept (not deduped away)...
        assert len(result) == 2
        gids = {gid for _, _, gid, _ in result}
        assert gids == {result[0][2]} and result[0][2] is not None  # one shared group
        # ...with exactly one primary: the higher-confidence entry.
        primaries = [e for e, _, _, primary in result if primary]
        assert len(primaries) == 1
        assert primaries[0]["confidence"] == 0.95

    def test_empty_codes_stand_alone(self) -> None:
        entries = [
            ({"error_code": "", "confidence": 0.5}, "chunk1"),
            ({"error_code": "", "confidence": 0.6}, "chunk2"),
        ]
        result = _group_duplicates(entries, KnowledgeType.ALARM, "f.pdf")
        assert len(result) == 2
        assert all(gid is None and primary for _, _, gid, primary in result)


class TestGroupDuplicates:
    def test_setup_groups_by_station(self) -> None:
        entries = [
            ({"station": "Loader 1", "procedure": "Step A then B", "confidence": 0.7}, "c1"),
            ({"station": "Loader 1", "procedure": "Step A then B", "confidence": 0.9}, "c2"),
            ({"station": "Unloader", "procedure": "X", "confidence": 0.8}, "c3"),
        ]
        result = _group_duplicates(entries, KnowledgeType.SETUP, "f.pdf")
        # All three retained; the two Loader 1 variants share a group.
        assert len(result) == 3
        loader = [(e, gid, p) for e, _, gid, p in result if e["station"] == "Loader 1"]
        assert len({gid for _, gid, _ in loader}) == 1 and loader[0][1] is not None
        primary = [e for e, gid, p in loader if p]
        assert len(primary) == 1 and primary[0]["confidence"] == 0.9
        # The standalone Unloader is its own primary, ungrouped.
        unloader = next((gid, p) for e, _, gid, p in result if e["station"] == "Unloader")
        assert unloader == (None, True)

    def test_experience_groups_by_problem(self) -> None:
        entries = [
            ({"problem": "P1", "failure_desc": "leak", "confidence": 0.6}, "c1"),
            ({"problem": "P1", "failure_desc": "leak", "confidence": 0.9}, "c2"),
        ]
        result = _group_duplicates(entries, KnowledgeType.EXPERIENCE, "f.pdf")
        assert len(result) == 2
        primaries = [e for e, _, _, p in result if p]
        assert len(primaries) == 1 and primaries[0]["confidence"] == 0.9

    def test_empty_keys_stand_alone(self) -> None:
        entries = [
            ({"station": "", "procedure": "", "confidence": 0.5}, "c1"),
            ({"station": "", "procedure": "", "confidence": 0.6}, "c2"),
        ]
        result = _group_duplicates(entries, KnowledgeType.SETUP, "f.pdf")
        # Empty-keyed entries are never collapsed into a group.
        assert len(result) == 2
        assert all(gid is None and primary for _, _, gid, primary in result)


class TestParseJsonArray:
    def test_plain_array(self) -> None:
        out = _parse_json_array('[{"a": 1}, {"b": 2}]')
        assert out == [{"a": 1}, {"b": 2}]

    def test_markdown_fence(self) -> None:
        out = _parse_json_array('```json\n[{"a": 1}]\n```')
        assert out == [{"a": 1}]

    def test_single_object_promoted(self) -> None:
        out = _parse_json_array('{"a": 1}')
        assert out == [{"a": 1}]

    def test_truncated_recovers_prefix(self) -> None:
        # LLM ran out of tokens partway through the third entry
        truncated = '[{"a": 1}, {"b": 2}, {"c": "incomp'
        out = _parse_json_array(truncated)
        assert out == [{"a": 1}, {"b": 2}]

    def test_control_chars_stripped(self) -> None:
        out = _parse_json_array('[{"a": "ok\x00here"}]')
        assert out == [{"a": "okhere"}]

    def test_leading_prose(self) -> None:
        out = _parse_json_array('Here is the array:\n[{"a": 1}]')
        assert out == [{"a": 1}]


class TestSplitOversizedPage:
    def test_small_page_unchanged(self) -> None:
        page = (3, "short content")
        assert _split_oversized_page(page, max_chars=100) == [page]

    def test_preserves_page_number(self) -> None:
        # Build a page with multiple heading-like sections, well over budget
        text = "\n\n".join(f"1.{i} Section heading\n" + ("body " * 200) for i in range(1, 6))
        sub_pages = _split_oversized_page((7, text), max_chars=400)
        assert len(sub_pages) > 1
        assert all(p[0] == 7 for p in sub_pages)
        assert all(len(p[1]) <= 800 for p in sub_pages)  # some slack for joining

    def test_hard_cut_when_no_separators(self) -> None:
        # Single massive run with no breaks at all
        text = "x" * 5000
        sub_pages = _split_oversized_page((1, text), max_chars=1000)
        assert len(sub_pages) >= 5
        assert all(p[0] == 1 for p in sub_pages)

    def test_preamble_before_first_heading_is_kept(self) -> None:
        # Regression: content above the first detected heading must not be
        # dropped. A table-rendered alarm row ("| 300406 | … |") starts with
        # "|", so the heading regex skips it; the first heading it matches is a
        # later plain-text alarm code. Everything before that — including 300406
        # and 300410 — used to be silently discarded, leaving the file with
        # fewer (or zero) extracted documents.
        preamble = (
            "Drive and I/O alarms\n"
            "| 300406 | Problem in the non-cyclic communication |\n"
            + ("| Definitions: | " + "x " * 200 + "|\n")
            + "| 300410 | Axis error when storing a file |\n"
            + ("| Remedy: | " + "y " * 200 + "|\n")
        )
        body = "\n\n".join(
            f"30041{i}\tAxis error number {i}\n" + ("detail " * 200)
            for i in range(1, 5)
        )
        text = preamble + body
        sub_pages = _split_oversized_page((1, text), max_chars=1500)
        joined = "\n\n".join(t for _, t in sub_pages)
        assert "300406" in joined
        assert "300410" in joined
        assert all(p[0] == 1 for p in sub_pages)
