"""OCR wrapper for scanned PDF pages.

Uses PaddleOCR for best CJK accuracy. Falls back gracefully if PaddleOCR
is not installed — returns empty string with a warning.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("kb.ocr")

_ocr_instance: Any = None
_ocr_init_attempted = False


def _get_ocr() -> Any:
    global _ocr_instance, _ocr_init_attempted
    if _ocr_init_attempted:
        return _ocr_instance
    _ocr_init_attempted = True
    try:
        from paddleocr import PaddleOCR
        _ocr_instance = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        log.info("PaddleOCR initialized successfully")
    except ImportError:
        log.warning("PaddleOCR not installed — OCR fallback disabled")
    except Exception as exc:
        log.warning("PaddleOCR init failed — %s", exc)
    return _ocr_instance


def ocr_page_image(image_bytes: bytes) -> str:
    """Run OCR on a PNG image (as bytes). Returns extracted text or empty string."""
    ocr = _get_ocr()
    if ocr is None:
        return ""

    try:
        import io

        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        img_array = np.array(img)

        result = ocr.ocr(img_array, cls=True)
        if not result or not result[0]:
            return ""

        lines: list[str] = []
        for line in result[0]:
            if line and len(line) >= 2:
                text = line[1][0] if isinstance(line[1], (list, tuple)) else str(line[1])
                lines.append(text)
        return "\n".join(lines)

    except Exception as exc:
        log.warning("OCR failed for page image: %s", exc)
        return ""
