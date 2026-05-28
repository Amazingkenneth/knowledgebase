"""Unit tests for file extraction and related utilities."""

from __future__ import annotations

import csv
import io
import tempfile
from pathlib import Path

import pytest

from kb.services.extraction import extract_csv, extract_file, EXTRACTORS
from kb.services.segmentation import (
    chunk_pages,
    verify_extraction_fidelity,
    _deduplicate_alarms_with_context,
    _deduplicate_entries,
    _parse_json_array,
    _split_oversized_page,
)
from kb.models.taxonomy import KnowledgeType


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
        # Overlap: last page of chunk N should be first page of chunk N+1
        for i in range(len(chunks) - 1):
            assert chunks[i][-1] == chunks[i + 1][0]

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


class TestDeduplicateAlarms:
    def test_no_duplicates(self) -> None:
        entries = [
            ({"error_code": "E1001", "confidence": 0.9}, "chunk1"),
            ({"error_code": "E1002", "confidence": 0.8}, "chunk2"),
        ]
        result = _deduplicate_alarms_with_context(entries)
        assert len(result) == 2

    def test_keeps_higher_confidence(self) -> None:
        entries = [
            ({"error_code": "E1001", "confidence": 0.7}, "chunk1"),
            ({"error_code": "E1001", "confidence": 0.95}, "chunk2"),
        ]
        result = _deduplicate_alarms_with_context(entries)
        assert len(result) == 1
        assert result[0][0]["confidence"] == 0.95

    def test_handles_empty_codes(self) -> None:
        entries = [
            ({"error_code": "", "confidence": 0.5}, "chunk1"),
            ({"error_code": "", "confidence": 0.6}, "chunk2"),
        ]
        result = _deduplicate_alarms_with_context(entries)
        assert len(result) == 2


class TestUniversalDedup:
    def test_setup_dedups_by_station(self) -> None:
        entries = [
            ({"station": "Loader 1", "procedure": "Step A then B", "confidence": 0.7}, "c1"),
            ({"station": "Loader 1", "procedure": "Step A then B", "confidence": 0.9}, "c2"),
            ({"station": "Unloader", "procedure": "X", "confidence": 0.8}, "c3"),
        ]
        result = _deduplicate_entries(entries, KnowledgeType.SETUP)
        stations = sorted(e["station"] for e, _ in result)
        assert stations == ["Loader 1", "Unloader"]
        # Higher-confidence Loader 1 wins
        loader = next(e for e, _ in result if e["station"] == "Loader 1")
        assert loader["confidence"] == 0.9

    def test_experience_dedups_by_problem(self) -> None:
        entries = [
            ({"problem": "P1", "failure_desc": "leak", "confidence": 0.6}, "c1"),
            ({"problem": "P1", "failure_desc": "leak", "confidence": 0.9}, "c2"),
        ]
        result = _deduplicate_entries(entries, KnowledgeType.EXPERIENCE)
        assert len(result) == 1
        assert result[0][0]["confidence"] == 0.9

    def test_empty_keys_pass_through(self) -> None:
        entries = [
            ({"station": "", "procedure": "", "confidence": 0.5}, "c1"),
            ({"station": "", "procedure": "", "confidence": 0.6}, "c2"),
        ]
        result = _deduplicate_entries(entries, KnowledgeType.SETUP)
        # Both kept — empty-keyed entries are not collapsed into each other
        assert len(result) == 2


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
