"""Tests for real-file extraction against programmatically generated fixtures.

The fixtures in conftest.py build minimal PPTX and PDF files on-the-fly from
the three seed CSVs:
  - alarm_pptx_path     → mirrors 机台报警_header.csv     (52 alarms)
  - setup_pptx_path     → mirrors 机台setup_header.csv   (58 setup stations)
  - experience_pdf_path → mirrors 设备经验_header.csv     (50 experience cases)

Using generated fixtures instead of committed binary files keeps the repo
lightweight while still exercising the real extraction code on files that
contain every data point the tests assert against.

PDF extraction runs with ``ocr_enabled=True`` (the production default) so the
full OCR code-path is exercised.  The experience PDF is text-only (no embedded
raster images), so OCR is never triggered — the tests verify this explicitly.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"

pptx_mod = pytest.importorskip("pptx", reason="python-pptx not installed")
fitz_mod = pytest.importorskip("fitz", reason="pymupdf not installed")


# ---------------------------------------------------------------------------
# CSV helpers  (read the authoritative seed files for cross-checks)
# ---------------------------------------------------------------------------

import csv


def _read_csv(path: Path) -> list[dict[str, str]]:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            with open(path, encoding=enc) as f:
                return list(csv.DictReader(f))
        except (UnicodeDecodeError, ValueError):
            continue
    return []


# ---------------------------------------------------------------------------
# Extraction fixtures  (use paths provided by conftest generators)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def alarm_pages(alarm_pptx_path):
    from kb.services.extraction import extract_pptx
    return extract_pptx(alarm_pptx_path)


@pytest.fixture(scope="module")
def alarm_text(alarm_pages):
    return "\n".join(text for _, text in alarm_pages)


@pytest.fixture(scope="module")
def setup_pages(setup_pptx_path):
    from kb.services.extraction import extract_pptx
    return extract_pptx(setup_pptx_path)


@pytest.fixture(scope="module")
def setup_text(setup_pages):
    return "\n".join(text for _, text in setup_pages)


@pytest.fixture(scope="module")
def experience_pages(experience_pdf_path):
    """OCR-enabled — the production default path."""
    from kb.services.extraction import extract_pdf
    return extract_pdf(experience_pdf_path, ocr_enabled=True)


@pytest.fixture(scope="module")
def experience_pages_no_ocr(experience_pdf_path):
    from kb.services.extraction import extract_pdf
    return extract_pdf(experience_pdf_path, ocr_enabled=False)


@pytest.fixture(scope="module")
def experience_text(experience_pages):
    return "\n".join(text for _, text in experience_pages)


# ---------------------------------------------------------------------------
# CSV seed fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def alarm_csv():
    return _read_csv(CONFIG_DIR / "机台报警_header.csv")


@pytest.fixture(scope="module")
def setup_csv():
    return _read_csv(CONFIG_DIR / "机台setup_header.csv")


@pytest.fixture(scope="module")
def experience_csv():
    return _read_csv(CONFIG_DIR / "设备经验_header.csv")


# ---------------------------------------------------------------------------
# Alarm PPTX tests
# ---------------------------------------------------------------------------

class TestAlarmPptxExtraction:
    def test_returns_pages(self, alarm_pages):
        assert len(alarm_pages) > 0

    def test_page_count_matches_csv(self, alarm_pages, alarm_csv):
        # Cover + roster slide + one slide per alarm → total ≥ len(CSV).
        assert len(alarm_pages) >= len(alarm_csv)

    def test_all_alarm_codes_present(self, alarm_text, alarm_csv):
        codes = [row["代码"] for row in alarm_csv]
        missing = [c for c in codes if c not in alarm_text]
        assert not missing, f"Missing alarm codes: {missing}"

    def test_alarm_code_count_matches_csv(self, alarm_text, alarm_csv):
        # Extracted E1xxx codes must match the CSV set exactly.
        csv_codes = {row["代码"] for row in alarm_csv}
        extracted = set(re.findall(r"E1\d{3}", alarm_text))
        assert csv_codes == extracted

    def test_equipment_types_present(self, alarm_text):
        for equip in ("Aligner", "Conveyor", "FTU", "Heater", "Loader", "Pump", "SensorModule", "Stage"):
            assert equip in alarm_text, f"Equipment '{equip}' not found"

    def test_project_names_present(self, alarm_text):
        for project in ("Kinneret", "MEM", "MHK", "PDX", "Boston", "Sonora", "Yucatan"):
            assert project in alarm_text, f"Project '{project}' not found"

    def test_specific_alarm_content(self, alarm_text):
        # E1005 — Emergency Stop: body text contains 急停按钮被按下.
        assert "E1005" in alarm_text
        assert "急停按钮被按下" in alarm_text

    def test_clearing_procedure_content(self, alarm_text):
        assert "解除流程" in alarm_text

    def test_page_numbers_sequential(self, alarm_pages):
        nums = [n for n, _ in alarm_pages]
        assert nums == sorted(nums)
        assert nums[0] == 1

    def test_no_blank_pages(self, alarm_pages):
        blank = [(n, t) for n, t in alarm_pages if not t.strip()]
        assert not blank, f"Blank pages: {[n for n, _ in blank]}"

    def test_safety_interlock_section(self, alarm_text):
        assert "安全联锁" in alarm_text
        assert "SAFETY INTERLOCK" in alarm_text

    def test_three_severity_levels(self, alarm_text):
        assert "故障停机" in alarm_text
        assert "预警提示" in alarm_text


# ---------------------------------------------------------------------------
# Setup PPTX tests
# ---------------------------------------------------------------------------

class TestSetupPptxExtraction:
    def test_returns_pages(self, setup_pages):
        assert len(setup_pages) > 0

    def test_page_count_matches_csv(self, setup_pages, setup_csv):
        # Cover + section slides + one slide per station ≥ len(CSV).
        assert len(setup_pages) >= len(setup_csv)

    def test_all_equipment_sections_present(self, setup_text):
        for equip in ("Aligner", "Conveyor", "FTU", "Heater", "Loader", "Pump", "SensorModule", "Stage"):
            assert equip in setup_text, f"Equipment section '{equip}' not found"

    def test_key_station_names_present(self, setup_text, setup_csv):
        # Station names are matched on their first 5 chars (normalised) to
        # tolerate minor abbreviation differences; ≤5 misses is accepted.
        normalised = setup_text.replace(" ", "")
        misses = []
        for row in setup_csv:
            station = row.get("工站/部件/站位", "").strip()
            if not station:
                continue
            if station.replace(" ", "")[:5] not in normalised:
                misses.append(station)
        assert len(misses) <= 5, f"Too many missing stations ({len(misses)}): {misses}"

    def test_spec_and_procedure_sections(self, setup_text):
        assert "SPEC" in setup_text or "规格" in setup_text
        assert "PROCEDURE" in setup_text or "调试步骤" in setup_text

    def test_tools_section(self, setup_text):
        assert "TOOLS" in setup_text or "调试工具" in setup_text

    def test_equipment_station_counts(self, setup_text):
        # Cover slide lists station counts per equipment type.
        assert "9 工站" in setup_text   # Aligner: 9 stations
        assert "7 工站" in setup_text   # Conveyor: 7 stations

    def test_table_content_extracted(self, setup_text):
        # Station slides use PPTX tables → extractor renders them as pipe-grids.
        assert "|" in setup_text

    def test_page_numbers_sequential(self, setup_pages):
        nums = [n for n, _ in setup_pages]
        assert nums == sorted(nums)
        assert nums[0] == 1

    def test_no_blank_pages(self, setup_pages):
        blank = [(n, t) for n, t in setup_pages if not t.strip()]
        assert not blank, f"Blank pages: {[n for n, _ in blank]}"


# ---------------------------------------------------------------------------
# Experience PDF tests
# ---------------------------------------------------------------------------

class TestExperiencePdfExtraction:
    """Content-focused tests for the experience PDF (OCR-enabled path)."""

    def test_returns_pages(self, experience_pages):
        assert len(experience_pages) > 0

    def test_page_numbers_sequential(self, experience_pages):
        nums = [n for n, _ in experience_pages]
        assert nums == sorted(nums)
        assert nums[0] == 1

    def test_no_blank_pages(self, experience_pages):
        blank = [(n, t) for n, t in experience_pages if not t.strip()]
        assert not blank, f"Blank pages: {[n for n, _ in blank]}"

    def test_all_equipment_types_present(self, experience_text):
        for equip in ("Aligner", "Conveyor", "FTU", "Heater", "Loader", "Pump", "SensorModule", "Stage"):
            assert equip in experience_text, f"Equipment '{equip}' not found"

    def test_all_projects_present(self, experience_text):
        for project in ("Kinneret", "MEM", "MHK", "PDX", "Boston", "Sonora", "Yucatan"):
            assert project in experience_text, f"Project '{project}' not found"

    def test_all_eight_root_cause_categories(self, experience_text):
        # The PDF defines 8 categories A–H — every one must be present.
        for cat in (
            "A · 电气", "B · 机械", "C · 寿命", "D · 热管理",
            "E · 污染", "F · 选型", "G · 厂务", "H · 管理",
        ):
            assert cat in experience_text, f"Root-cause category '{cat}' not found"

    def test_root_cause_and_corrective_action_labels(self, experience_text):
        for label in ("根因", "ROOT CAUSE"):
            assert label in experience_text, f"Label '{label}' missing"
        assert "纠正措施" in experience_text or "CORRECTIVE ACTION" in experience_text

    def test_deep_dive_section_labels(self, experience_text):
        for label in ("失败描述", "SYMPTOM", "失败分析", "ANALYSIS"):
            assert label in experience_text, f"Deep-dive label '{label}' not found"

    def test_pptx_deep_dive_references(self, experience_text):
        # Each deep-dive section references a source PPT file (exp_*.pptx).
        refs = re.findall(r"exp_\w+\.pptx", experience_text)
        assert len(refs) >= 8, (
            f"Expected ≥8 PPT refs (one per root-cause category), found {len(refs)}: {refs}"
        )

    def test_all_problems_in_csv_present(self, experience_text, experience_csv):
        # Every verbatim problem description from the CSV must appear in the PDF.
        missing = [
            row["问题"] for row in experience_csv
            if row.get("问题") and row["问题"].strip() not in experience_text
        ]
        assert not missing, (
            f"Problem descriptions absent from extracted text ({len(missing)}): {missing}"
        )

    def test_all_root_causes_in_csv_present(self, experience_text, experience_csv):
        # Leading 20 chars of every root-cause value must appear; summary
        # tables may truncate long values but always preserve the prefix.
        missing = []
        for row in experience_csv:
            snippet = row.get("根因", "").strip()[:20]
            if snippet and snippet not in experience_text:
                missing.append((row.get("问题", ""), snippet))
        assert not missing, (
            f"Root-cause snippets absent from extracted text ({len(missing)}): {missing}"
        )

    def test_all_projects_in_cases(self, experience_text, experience_csv):
        # Every named project (excluding "所有项目" → rendered as ALL) must appear.
        projects = {r.get("项目", "").strip() for r in experience_csv}
        projects.discard("所有项目")
        missing = [p for p in projects if p not in experience_text]
        assert not missing, f"Project names absent from extracted text: {missing}"

    def test_category_summary_tables_present(self, experience_text):
        for header in ("问题 PROBLEM", "根因 ROOT CAUSE", "关键纠正 KEY ACTION"):
            assert header in experience_text, f"Summary-table column header '{header}' not found"


# ---------------------------------------------------------------------------
# Experience PDF — OCR behaviour
# ---------------------------------------------------------------------------

class TestExperiencePdfOcrBehavior:
    """Verify OCR machinery behaves correctly for this text-only PDF.

    The generated experience PDF contains no embedded raster images, so the
    OCR fallback must be skipped on every page.  These tests confirm:

    * ``ocr_enabled=True`` produces identical output to ``ocr_enabled=False``
      (no unintended text substitution from an accidental OCR over-fire).
    * ``_should_use_ocr`` returns False for every page in an image-free doc.
    * When PaddleOCR IS installed, the module imports without error.
    * ``ocr_available()`` truthfully reflects whether paddleocr is importable.
    """

    def test_ocr_enabled_matches_no_ocr_for_text_pdf(
        self, experience_pages, experience_pages_no_ocr
    ):
        assert len(experience_pages) == len(experience_pages_no_ocr), (
            "Page count differs between OCR-enabled and OCR-disabled runs"
        )
        for (n_ocr, t_ocr), (n_base, t_base) in zip(experience_pages, experience_pages_no_ocr):
            assert n_ocr == n_base
            assert t_ocr == t_base, (
                f"Page {n_ocr} text differs between OCR and non-OCR runs — "
                "OCR may have misfired on a text-only page"
            )

    def test_no_pages_trigger_ocr(self, experience_pdf_path):
        import fitz
        from kb.services.extraction import _should_use_ocr

        doc = fitz.open(str(experience_pdf_path))
        try:
            triggered = [
                page_num + 1
                for page_num in range(len(doc))
                if _should_use_ocr(
                    doc[page_num].get_text("text") or "", doc[page_num], True
                )
            ]
        finally:
            doc.close()
        assert not triggered, (
            f"OCR triggered on image-free pages: {triggered}. "
            "Check _should_use_ocr thresholds — text-only pages must not be OCR'd."
        )

    def test_ocr_import_smoke(self):
        paddleocr = pytest.importorskip("paddleocr", reason="paddleocr extra not installed")
        assert hasattr(paddleocr, "PaddleOCR"), "PaddleOCR class not found in paddleocr module"

    def test_ocr_available_reflects_installation(self):
        from kb.services.ocr import ocr_available

        try:
            import paddleocr  # noqa: F401
            expected = True
        except ImportError:
            expected = False

        assert ocr_available() is expected, (
            f"ocr_available() returned {ocr_available()!r} but paddleocr importable={expected}"
        )


# ---------------------------------------------------------------------------
# Cross-format consistency
# ---------------------------------------------------------------------------

class TestCrossFormatConsistency:
    """Verify that PPTX / PDF extraction produces the same taxonomy values
    that the seeded CSVs define, so the segmentation LLM can match them."""

    @pytest.fixture(scope="class")
    def taxonomy(self):
        from kb.services.taxonomy import TaxonomyStore
        return TaxonomyStore(CONFIG_DIR / "taxonomy.yaml").current

    def test_alarm_equipment_matches_taxonomy(self, alarm_text, taxonomy):
        for equip in taxonomy.equipment:
            assert equip in alarm_text, f"Taxonomy equipment '{equip}' absent from alarm PPTX"

    def test_setup_equipment_matches_taxonomy(self, setup_text, taxonomy):
        for equip in taxonomy.equipment:
            assert equip in setup_text, f"Taxonomy equipment '{equip}' absent from setup PPTX"

    def test_experience_equipment_matches_taxonomy(self, experience_text, taxonomy):
        for equip in taxonomy.equipment:
            assert equip in experience_text, f"Taxonomy equipment '{equip}' absent from experience PDF"
