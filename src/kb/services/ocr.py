"""OCR wrapper for scanned PDF pages.

Uses PaddleOCR for best CJK accuracy. The "ch" model also recognizes Latin
script, so English model numbers / units in otherwise-Chinese documents are
handled — set ``lang`` to ``en`` for English-only docs. Falls back gracefully
if PaddleOCR is not installed — returns empty string with a warning.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("kb.ocr")

# One engine per language — initializing PaddleOCR is heavy (~seconds + RAM),
# so we cache an instance per requested language for the server lifetime.
_ocr_instances: dict[str, Any] = {}
_ocr_attempted: set[str] = set()
_paddle_importable: bool | None = None


def ocr_available() -> bool:
    """Whether PaddleOCR can be imported at all (independent of language).

    Lets callers distinguish "scanned PDF but OCR not installed" from "OCR ran
    but found nothing", so the user gets an actionable message.
    """
    global _paddle_importable
    if _paddle_importable is None:
        try:
            import paddleocr  # noqa: F401

            _paddle_importable = True
        except ImportError:
            _paddle_importable = False
    return _paddle_importable


def _get_ocr(lang: str) -> Any:
    if lang in _ocr_attempted:
        return _ocr_instances.get(lang)
    _ocr_attempted.add(lang)
    try:
        from paddleocr import PaddleOCR

        _ocr_instances[lang] = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)
        log.info("PaddleOCR initialized successfully (lang=%s)", lang)
    except ImportError:
        log.warning("PaddleOCR not installed — OCR fallback disabled")
    except Exception as exc:
        log.warning("PaddleOCR init failed (lang=%s) — %s", lang, exc)
    return _ocr_instances.get(lang)


def ocr_page_image(
    image_bytes: bytes,
    *,
    lang: str = "ch",
    min_confidence: float = 0.5,
) -> str:
    """Run OCR on a PNG image (as bytes). Returns extracted text or empty string.

    Lines whose recognition confidence is below ``min_confidence`` are dropped
    so low-quality scans don't feed garbage tokens into the segmentation LLM.
    """
    ocr = _get_ocr(lang)
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
            if not line or len(line) < 2:
                continue
            payload = line[1]
            # PaddleOCR returns (text, confidence) per line; tolerate older
            # shapes where line[1] is a bare string.
            if isinstance(payload, (list, tuple)):
                text = str(payload[0])
                conf = float(payload[1]) if len(payload) > 1 else 1.0
            else:
                text = str(payload)
                conf = 1.0
            if conf >= min_confidence and text.strip():
                lines.append(text)
        return "\n".join(lines)

    except Exception as exc:
        log.warning("OCR failed for page image: %s", exc)
        return ""
