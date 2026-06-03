"""Resource-limit guards in extraction: a pathological file is rejected with a
clear ExtractionLimitError instead of risking an OOM.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kb.services.extraction import (
    ExtractionLimitError,
    extract_file,
    extract_pdf,
    extract_xlsx,
)


def _make_pdf(path: Path, pages: int) -> None:
    fitz = pytest.importorskip("fitz", reason="pymupdf not installed")
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((40, 40), f"page {i}")
    doc.save(str(path))
    doc.close()


def _make_xlsx(path: Path, rows: int, cols: int) -> None:
    pytest.importorskip("openpyxl", reason="openpyxl not installed")
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for _ in range(rows):
        ws.append([f"c{j}" for j in range(cols)])
    wb.save(str(path))


class TestPdfPageLimit:
    def test_over_limit_raises(self, tmp_path: Path) -> None:
        pdf = tmp_path / "big.pdf"
        _make_pdf(pdf, pages=5)
        with pytest.raises(ExtractionLimitError, match="exceeding the limit"):
            extract_pdf(pdf, max_pages=3)

    def test_at_limit_ok(self, tmp_path: Path) -> None:
        pdf = tmp_path / "ok.pdf"
        _make_pdf(pdf, pages=3)
        pages = extract_pdf(pdf, max_pages=3)
        assert len(pages) == 3

    def test_extract_file_threads_pdf_limit(self, tmp_path: Path) -> None:
        pdf = tmp_path / "big.pdf"
        _make_pdf(pdf, pages=5)
        with pytest.raises(ExtractionLimitError):
            extract_file(pdf, pdf_max_pages=2)


class TestXlsxCellLimit:
    def test_over_limit_raises(self, tmp_path: Path) -> None:
        xlsx = tmp_path / "big.xlsx"
        _make_xlsx(xlsx, rows=50, cols=10)  # 500 cells
        with pytest.raises(ExtractionLimitError, match="cell limit"):
            extract_xlsx(xlsx, max_cells=100)

    def test_under_limit_ok(self, tmp_path: Path) -> None:
        xlsx = tmp_path / "ok.xlsx"
        _make_xlsx(xlsx, rows=3, cols=3)
        pages = extract_xlsx(xlsx, max_cells=1000)
        assert pages and "c0" in pages[0][1]

    def test_extract_file_threads_xlsx_limit(self, tmp_path: Path) -> None:
        xlsx = tmp_path / "big.xlsx"
        _make_xlsx(xlsx, rows=50, cols=10)
        with pytest.raises(ExtractionLimitError):
            extract_file(xlsx, xlsx_max_cells=50)
