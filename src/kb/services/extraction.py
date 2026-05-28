"""File-type-specific text extraction, preserving page boundaries.

Each extractor returns a list of (page_number, text) tuples. For flat formats
(CSV, XLSX) each row or sheet maps to a "page". For paginated formats (PDF,
PPTX, DOCX) the page/slide/section index is preserved.

Direct text extraction is attempted first; OCR is used as fallback when a
page yields little text but contains images (scanned content).
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("kb.extraction")

PageText = tuple[int, str]  # (page_number, text)


def _try_import(module: str) -> Any:
    """Lazy-import an optional dependency, returning None if missing."""
    try:
        import importlib
        return importlib.import_module(module)
    except ImportError:
        return None


# ── PDF extraction ───────────────────────────────────────────────────────────

def extract_pdf(path: Path, *, ocr_enabled: bool = True) -> list[PageText]:
    fitz = _try_import("fitz")
    if fitz is None:
        raise ImportError("pymupdf is required for PDF extraction: pip install pymupdf")

    pages: list[PageText] = []
    doc = fitz.open(str(path))
    try:
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text("text") or ""

            if _should_use_ocr(text, page, ocr_enabled):
                from kb.services.ocr import ocr_page_image
                pix = page.get_pixmap(dpi=300)
                img_bytes = pix.tobytes("png")
                ocr_text = ocr_page_image(img_bytes)
                if len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text

            if text.strip():
                pages.append((page_num + 1, text))
    finally:
        doc.close()
    return pages


def _should_use_ocr(text: str, page: Any, ocr_enabled: bool) -> bool:
    if not ocr_enabled:
        return False
    text_chars = len(text.strip())
    image_list = page.get_images(full=True)
    if text_chars < 50 and len(image_list) > 0:
        return True
    if text_chars > 0:
        printable_ratio = sum(1 for c in text if c.isprintable() or c in "\n\r\t") / len(text)
        if printable_ratio < 0.7:
            return True
    return False


# ── PPTX extraction ─────────────────────────────────────────────────────────

def extract_pptx(path: Path) -> list[PageText]:
    pptx_mod = _try_import("pptx")
    if pptx_mod is None:
        raise ImportError("python-pptx is required for PPTX extraction: pip install python-pptx")

    from pptx import Presentation

    pages: list[PageText] = []
    prs = Presentation(str(path))
    for slide_num, slide in enumerate(prs.slides, start=1):
        texts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    line = paragraph.text.strip()
                    if line:
                        texts.append(line)
            if shape.has_table:
                table = shape.table
                for row in table.rows:
                    row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if row_texts:
                        texts.append("\t".join(row_texts))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                texts.append(f"[Notes] {notes}")
        if texts:
            pages.append((slide_num, "\n".join(texts)))
    return pages


# ── DOCX extraction ─────────────────────────────────────────────────────────

def extract_docx(path: Path) -> list[PageText]:
    docx_mod = _try_import("docx")
    if docx_mod is None:
        raise ImportError("python-docx is required for DOCX extraction: pip install python-docx")

    from docx import Document

    doc = Document(str(path))
    texts: list[str] = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t:
            texts.append(t)
    for table in doc.tables:
        for row in table.rows:
            row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if row_texts:
                texts.append("\t".join(row_texts))
    full_text = "\n".join(texts)
    if not full_text.strip():
        return []
    return [(1, full_text)]


# ── XLSX / XLS extraction ───────────────────────────────────────────────────

def extract_xlsx(path: Path) -> list[PageText]:
    openpyxl = _try_import("openpyxl")
    if openpyxl is None:
        raise ImportError("openpyxl is required for XLSX extraction: pip install openpyxl")

    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    pages: list[PageText] = []
    for sheet_idx, sheet_name in enumerate(wb.sheetnames, start=1):
        ws = wb[sheet_name]
        rows: list[str] = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() if c is not None else "" for c in row]
            line = "\t".join(cells)
            if line.replace("\t", "").strip():
                rows.append(line)
        if rows:
            pages.append((sheet_idx, "\n".join(rows)))
    wb.close()
    return pages


# ── CSV extraction ───────────────────────────────────────────────────────────

def extract_csv(path: Path) -> list[PageText]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except (UnicodeDecodeError, ValueError):
            continue
    else:
        return []

    reader = csv.reader(io.StringIO(text))
    rows: list[str] = []
    for row in reader:
        line = "\t".join(c.strip() for c in row)
        if line.replace("\t", "").strip():
            rows.append(line)
    if not rows:
        return []
    return [(1, "\n".join(rows))]


# ── Dispatcher ───────────────────────────────────────────────────────────────

EXTRACTORS = {
    "pdf": extract_pdf,
    "pptx": extract_pptx,
    "docx": extract_docx,
    "xlsx": extract_xlsx,
    "xls": extract_xlsx,
    "csv": extract_csv,
}


def extract_file(path: Path, *, ocr_enabled: bool = True) -> list[PageText]:
    """Extract text from a file, returning (page_number, text) pairs.

    Raises ImportError if the required library is not installed.
    Raises ValueError for unsupported file types.
    """
    suffix = path.suffix.lower().lstrip(".")
    extractor = EXTRACTORS.get(suffix)
    if extractor is None:
        raise ValueError(f"Unsupported file type: {suffix}")

    if suffix == "pdf":
        return extract_pdf(path, ocr_enabled=ocr_enabled)
    return extractor(path)
