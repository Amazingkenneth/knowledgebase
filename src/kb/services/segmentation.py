"""LLM-based document segmentation — splits extracted text into structured documents.

The LLM acts as a structuring/parsing tool ONLY. It identifies boundaries
between alarm codes, setup procedures, or experience entries and maps each
segment to the document model fields. It must copy text verbatim — never
paraphrase or fabricate content.

Robustness layers (outer → inner):
  1. Structural chunking: prefer heading/paragraph boundaries over hard
     char-count cuts; split oversized single pages instead of overflowing.
  2. JSON salvage: extract the longest valid JSON-array prefix from a
     partial / truncated LLM response before declaring failure.
  3. Repair retry: on JSON failure, ask the model once to re-emit valid JSON.
  4. Binary-split fallback: if a chunk still fails (typically because a
     single entry exceeds max_tokens), split the chunk in half on a page
     boundary and recurse. Down to one page, then one structural block.
  5. Universal dedup across overlapping chunks (per knowledge type).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

import httpx

from kb.config import Settings
from kb.models.ingest import SkippedChunk, StagedDocument
from kb.models.taxonomy import KnowledgeType
from kb.services.extraction import PageText
from kb.services.spec import (
    TypeSpec,
    load_specs,
    render_router_prompt,
    render_segmentation_prompt,
)

log = logging.getLogger("kb.segmentation")

_OVERLAP_PAGES = 1
_MIN_SUBCHUNK_PAGES = 1  # recursion floor for binary-split fallback
# Chunks whose top-confidence entry is below this threshold are surfaced as
# low-confidence skips so the user can review them rather than silently dropping.
_LOW_CONFIDENCE_FLOOR = 0.3


# ── LLM call ─────────────────────────────────────────────────────────────────

# CJK = roughly 1.5 tokens/char; Latin = roughly 0.25 tokens/char (≈4 chars/token).
# Mixed-script estimator avoids over- or under-budgeting timeouts for either kind.
_CJK_RE = re.compile(r"[　-鿿＀-￯]")


def _estimate_tokens(text: str) -> float:
    if not text:
        return 0.0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk * 1.5 + other * 0.25


def _estimate_timeout(messages: list[dict[str, str]], max_tokens: int) -> float:
    """Derive a read-timeout from the actual message payload.

    Output is bounded by max_tokens but expected to be ~25% of input for
    structured JSON extraction. Qwen throughput ≈ 80 tok/s; 20 s base.
    """
    input_tokens = sum(_estimate_tokens(m["content"]) for m in messages)
    expected_output_tokens = min(max_tokens, input_tokens * 0.25)
    return max(60.0, 20.0 + (input_tokens + expected_output_tokens) / 80.0)


async def _call_llm(
    settings: Settings,
    messages: list[dict[str, str]],
    max_tokens: int | None = None,
) -> str:
    if not settings.llm.api_key:
        raise RuntimeError("LLM not configured — set KB_LLM__API_KEY")
    resolved_max_tokens = max_tokens or settings.ingest.segmentation_max_tokens
    timeout = _estimate_timeout(messages, resolved_max_tokens)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            settings.llm.api_url,
            headers={"Authorization": f"Bearer {settings.llm.api_key}"},
            json={
                "model": settings.llm.model,
                "messages": messages,
                "max_tokens": resolved_max_tokens,
                "stream": False,
            },
        )
    if resp.status_code != 200:
        log.warning("LLM segmentation error %s: %s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"LLM returned {resp.status_code}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return text.strip()


# Invalid JSON control chars: C0 controls except tab (\x09), LF (\x0a), CR (\x0d)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_json(text: str) -> str:
    """Strip control characters that are illegal inside JSON strings."""
    return _CONTROL_CHAR_RE.sub("", text)


# ── JSON parsing / salvage ───────────────────────────────────────────────────

def _parse_json_array(raw: str) -> list[dict[str, Any]]:
    """Parse the LLM response into a list of entry dicts.

    Tolerant of:
      - markdown code fences
      - leading prose ("Here is the JSON:")
      - illegal control chars inside strings
      - truncated output (salvages the longest valid array prefix)
      - a single object instead of an array
    Raises json.JSONDecodeError only if nothing salvageable remains.
    """
    cleaned = _sanitize_json(_strip_code_fence(raw))

    # Try the easy case first.
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return [e for e in parsed if isinstance(e, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    # Find the first '[' and try to salvage a valid prefix.
    start = cleaned.find("[")
    if start == -1:
        # No array at all — maybe a bare object?
        obj_start = cleaned.find("{")
        if obj_start != -1:
            salvaged = _salvage_json_object(cleaned[obj_start:])
            if salvaged is not None:
                return [salvaged]
        raise json.JSONDecodeError("No JSON array found", cleaned, 0)

    salvaged_list = _salvage_json_array(cleaned[start:])
    if salvaged_list is not None:
        return salvaged_list

    raise json.JSONDecodeError("Unrecoverable JSON", cleaned, start)


def _salvage_json_array(text: str) -> list[dict[str, Any]] | None:
    """Extract the longest parseable prefix of a JSON array.

    Walks the string tracking bracket/quote depth; whenever we land at
    depth-0 inside the outer array (i.e. just after a complete element),
    try parsing `text[:i] + "]"`. The last successful parse wins.

    This catches the common LLM truncation case where the response cuts
    off mid-element after several complete ones have already emitted.
    """
    last_good: list[dict[str, Any]] | None = None
    depth = 0
    in_str = False
    escape = False
    saw_outer_open = False

    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if in_str:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "[" or ch == "{":
            depth += 1
            if ch == "[" and not saw_outer_open:
                saw_outer_open = True
            continue
        if ch == "]" or ch == "}":
            depth -= 1
            # After closing an inner element back to depth 1 (inside outer
            # array), or fully closing the outer array, try a parse.
            if saw_outer_open and depth in (0, 1):
                candidate = text[: i + 1]
                if depth == 1:
                    # We're inside the outer array, mid-stream — close it.
                    candidate = candidate + "]"
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, list):
                        last_good = [e for e in parsed if isinstance(e, dict)]
                except json.JSONDecodeError:
                    pass
                if depth == 0:
                    break
    return last_good


def _salvage_json_object(text: str) -> dict[str, Any] | None:
    """Extract a single parseable JSON object from the front of text."""
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if in_str:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[: i + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


# ── Chunking ─────────────────────────────────────────────────────────────────

# Heading-ish markers that suggest a natural structural break.
_HEADING_RE = re.compile(
    r"^(?:"
    r"#{1,6}\s"                                 # markdown
    r"|第[一二三四五六七八九十百千]+[章节部分]"  # Chinese chapter
    r"|Chapter\s+\d+"                           # English chapter
    r"|\d+(?:\.\d+){0,3}\s+\S"                  # 1. / 1.2 / 1.2.3 numbered
    r"|[A-Z][A-Z0-9 _\-]{3,}$"                  # ALL-CAPS heading line
    r")",
    re.MULTILINE,
)


def _split_oversized_page(page: PageText, max_chars: int) -> list[PageText]:
    """Break a single oversized page into sub-pages on natural boundaries.

    Preference order:
      1. heading-like lines (chapters / numbered sections)
      2. blank-line paragraph breaks
      3. single-line breaks
      4. hard char-cut (last resort — only if a single line is huge)

    All sub-pages keep the original page number so source_pages stays correct.
    """
    page_num, text = page
    if len(text) <= max_chars:
        return [page]

    # Stage 1: heading-aware split.
    heading_positions = [m.start() for m in _HEADING_RE.finditer(text)]
    segments: list[str] = []
    if len(heading_positions) >= 2:
        bounds = heading_positions + [len(text)]
        for i in range(len(bounds) - 1):
            seg = text[bounds[i]: bounds[i + 1]].strip()
            if seg:
                segments.append(seg)
    else:
        segments = [text]

    # Stage 2: any segment still too big — split on paragraph breaks.
    refined: list[str] = []
    for seg in segments:
        if len(seg) <= max_chars:
            refined.append(seg)
            continue
        # Paragraph split, then line split, then hard cut.
        for piece in _split_on_separators(seg, max_chars, ("\n\n", "\n", "")):
            refined.append(piece)

    # Pack refined segments back into max_chars-bounded sub-pages.
    sub_pages: list[PageText] = []
    buf: list[str] = []
    buf_len = 0
    for seg in refined:
        seg_len = len(seg) + 2  # joining "\n\n"
        if buf and buf_len + seg_len > max_chars:
            sub_pages.append((page_num, "\n\n".join(buf)))
            buf, buf_len = [], 0
        buf.append(seg)
        buf_len += seg_len
    if buf:
        sub_pages.append((page_num, "\n\n".join(buf)))
    return sub_pages or [page]


def _split_on_separators(
    text: str, max_chars: int, separators: tuple[str, ...],
) -> list[str]:
    """Recursive separator split — tries each separator in turn.

    Empty-string separator falls through to a hard character cut.
    """
    if len(text) <= max_chars:
        return [text]
    sep = separators[0]
    if sep == "":
        # Hard cut.
        return [text[i: i + max_chars] for i in range(0, len(text), max_chars)]
    parts = text.split(sep)
    out: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for part in parts:
        plen = len(part) + len(sep)
        if buf and buf_len + plen > max_chars:
            out.append(sep.join(buf))
            buf, buf_len = [], 0
        if len(part) > max_chars:
            # Flush current, recurse on the over-sized part.
            if buf:
                out.append(sep.join(buf))
                buf, buf_len = [], 0
            out.extend(_split_on_separators(part, max_chars, separators[1:]))
            continue
        buf.append(part)
        buf_len += plen
    if buf:
        out.append(sep.join(buf))
    return out


def chunk_pages(pages: list[PageText], max_chars: int = 12000) -> list[list[PageText]]:
    """Split pages into chunks that fit within max_chars, with 1-page overlap.

    Pre-step: any single page exceeding max_chars is structurally subdivided
    so we never feed an over-budget chunk to the LLM. Sub-pages keep the
    original page number, so `source_pages` traceability is preserved.
    """
    if not pages:
        return []

    # Normalize: subdivide oversized pages first.
    normalized: list[PageText] = []
    for page in pages:
        normalized.extend(_split_oversized_page(page, max_chars))

    chunks: list[list[PageText]] = []
    current: list[PageText] = []
    current_len = 0

    for page in normalized:
        page_len = len(page[1])
        if current and current_len + page_len > max_chars:
            chunks.append(current)
            overlap = [current[-1]] if current else []
            current = overlap
            current_len = sum(len(p[1]) for p in current)
        current.append(page)
        current_len += page_len

    if current:
        chunks.append(current)
    return chunks


# ── Segmentation ─────────────────────────────────────────────────────────────

async def classify_chunk_types(
    settings: Settings,
    chunk_text: str,
    specs: dict[KnowledgeType, TypeSpec] | None = None,
) -> list[KnowledgeType]:
    """Classify a chunk → list of knowledge types present (may be more than one).

    Returns an empty list when the chunk is non-content (cover, TOC, preface).
    Returns multiple types when the chunk legitimately mixes types — e.g. an
    alarm description followed by its setup/calibration steps on the same page.
    The caller should then run each type's segmenter on the same chunk.
    """
    specs = specs or load_specs()
    sample = chunk_text[:2400]
    messages = [
        {"role": "system", "content": render_router_prompt(specs)},
        {"role": "user", "content": sample},
    ]
    try:
        raw = await _call_llm(settings, messages, max_tokens=60)
        parsed = json.loads(_strip_code_fence(raw))
    except Exception as exc:  # noqa: BLE001 — best-effort classification
        log.warning("Chunk classification failed: %s — defaulting to experience", exc)
        return [KnowledgeType.EXPERIENCE]

    # Accept both the multi-type shape {"types": [...]} and the legacy
    # single-type shape {"type": "..."}. Either way, normalize to a list.
    raw_types: list[str]
    if isinstance(parsed, dict) and isinstance(parsed.get("types"), list):
        raw_types = [str(t).strip().lower() for t in parsed["types"]]
    elif isinstance(parsed, dict) and "type" in parsed:
        raw_types = [str(parsed["type"]).strip().lower()]
    else:
        raw_types = []

    if not raw_types or "skip" in raw_types:
        return []

    out: list[KnowledgeType] = []
    for t in raw_types:
        try:
            kt = KnowledgeType(t)
            if kt not in out:
                out.append(kt)
        except ValueError:
            log.warning("Router returned unknown type %r — ignoring", t)
    return out or [KnowledgeType.EXPERIENCE]


async def classify_chunk_type(
    settings: Settings,
    chunk_text: str,
    specs: dict[KnowledgeType, TypeSpec] | None = None,
) -> KnowledgeType | None:
    """Back-compat single-type classifier. Returns the first detected type,
    or None for skip. New callers should prefer `classify_chunk_types`."""
    types = await classify_chunk_types(settings, chunk_text, specs)
    return types[0] if types else None


async def detect_knowledge_type(
    settings: Settings,
    sample_text: str,
    pages: list[PageText] | None = None,
) -> KnowledgeType:
    """Detect the dominant document type. Used as a hint for the UI; the
    per-chunk classifier in segment_text is the actual source of truth when
    no hint is locked.
    """
    sample = (
        _build_type_detection_sample(pages, char_budget=2400)
        if pages else sample_text[:2000]
    )
    types = await classify_chunk_types(settings, sample)
    return types[0] if types else KnowledgeType.EXPERIENCE


def _build_type_detection_sample(pages: list[PageText], char_budget: int) -> str:
    """Stitch head + middle + tail slices to expose the LLM to real content
    instead of just covers/TOCs at the front of the file."""
    if not pages:
        return ""
    per_slot = max(400, char_budget // 3)
    head = pages[0][1][:per_slot]
    parts = [f"--- pages start ---\n{head}"]
    if len(pages) >= 3:
        mid = pages[len(pages) // 2][1][:per_slot]
        parts.append(f"--- middle ---\n{mid}")
    if len(pages) >= 2:
        tail = pages[-1][1][:per_slot]
        parts.append(f"--- end ---\n{tail}")
    return "\n\n".join(parts)


async def segment_text(
    settings: Settings,
    pages: list[PageText],
    knowledge_type: KnowledgeType | None,
    file_name: str,
    project_hint: str | None = None,
    equipment_hint: str | None = None,
    on_chunk_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[StagedDocument], list[SkippedChunk]]:
    """Segment extracted text into structured documents.

    If `knowledge_type` is provided, it is **locked** — every chunk goes
    through that type's parser (use when the user explicitly chose a type).
    If `knowledge_type` is None, each chunk is classified independently:
    alarm / setup / experience / skip. Mixed-type files are supported and
    non-content pages (covers, TOCs, prefaces) are skipped with a friendly
    reason returned alongside.
    """
    if not pages:
        return [], []

    specs = load_specs()
    chunk_chars = settings.ingest.segmentation_chunk_chars
    chunks = chunk_pages(pages, max_chars=chunk_chars)

    full_raw_text = "\n\n".join(text for _, text in pages)
    normalized_full_raw = " ".join(full_raw_text.split())

    # Per-type buckets so dedup operates per knowledge type.
    parsed_by_type: dict[KnowledgeType, list[tuple[dict[str, Any], str]]] = {}
    skipped: list[SkippedChunk] = []
    total_chunks = len(chunks)

    for chunk_idx, chunk in enumerate(chunks):
        if on_chunk_progress:
            on_chunk_progress(chunk_idx + 1, total_chunks)

        page_range = _page_range(chunk)
        chunk_text = "\n\n".join(text for _, text in chunk)

        # Routing: when locked, every chunk goes through that single type.
        # Otherwise classify and possibly route to several types on the
        # SAME chunk (mixed-type chunks are real — e.g. an alarm with its
        # calibration steps inline).
        if knowledge_type is not None:
            chunk_types: list[KnowledgeType] = [knowledge_type]
        else:
            chunk_types = await classify_chunk_types(settings, chunk_text, specs)

        if not chunk_types:
            skipped.append(SkippedChunk(
                source_file=file_name,
                page_range=page_range,
                reason="non_content",
                hint=(
                    "Looks like a cover, table of contents, preface, or other "
                    "non-content page — nothing to extract here."
                ),
            ))
            log.info("Skipping non-content chunk %s in %s", page_range, file_name)
            continue

        per_type_results: dict[KnowledgeType, int] = {}
        for kt in chunk_types:
            entries = await _segment_chunk_with_fallback(
                settings, chunk, kt, file_name,
                project_hint, equipment_hint,
                spec=specs[kt],
                chunk_chars=chunk_chars,
            )
            per_type_results[kt] = len(entries)
            if entries:
                top_conf = max((e.get("confidence", 0) or 0) for e, _ in entries)
                if top_conf < _LOW_CONFIDENCE_FLOOR:
                    skipped.append(SkippedChunk(
                        source_file=file_name,
                        page_range=page_range,
                        reason="low_confidence",
                        hint=(
                            f"AI was unsure these pages contain {kt.value} entries "
                            f"(confidence {top_conf:.2f}). Entries are still included "
                            "below — please review carefully or reject."
                        ),
                    ))
                parsed_by_type.setdefault(kt, []).extend(entries)

        # Whole-chunk no-entry hint only if EVERY routed type came back empty.
        if all(n == 0 for n in per_type_results.values()):
            missed = "/".join(kt.value for kt in chunk_types)
            skipped.append(SkippedChunk(
                source_file=file_name,
                page_range=page_range,
                reason="no_entries",
                hint=(
                    f"AI thought these pages were {missed} but found no entries. "
                    "If you expected some, try setting a knowledge-type hint on "
                    "re-upload, or lower the chunk size in settings."
                ),
            ))

    # Dedup per type, then assemble.
    docs: list[StagedDocument] = []
    idx = 0
    for kt, entries in parsed_by_type.items():
        for entry, normalized_chunk in _deduplicate_entries(entries, kt):
            doc = _parsed_to_staged(
                idx, entry, kt, file_name,
                project_hint, equipment_hint,
                normalized_chunk, normalized_full_raw,
            )
            docs.append(doc)
            idx += 1

    return docs, skipped


def _page_range(chunk: list[PageText]) -> str:
    if not chunk:
        return "?"
    if len(chunk) == 1:
        return str(chunk[0][0])
    return f"{chunk[0][0]}-{chunk[-1][0]}"


async def _segment_chunk_with_fallback(
    settings: Settings,
    chunk: list[PageText],
    knowledge_type: KnowledgeType,
    file_name: str,
    project_hint: str | None,
    equipment_hint: str | None,
    chunk_chars: int,
    *,
    spec: TypeSpec | None = None,
    repair_attempt: bool = False,
) -> list[tuple[dict[str, Any], str]]:
    """Segment a single chunk with three layers of recovery.

    Order:
      1. First call → tolerant parse (salvages truncated arrays).
      2. If parse fails and no repair tried yet → repair-prompt retry.
      3. If still failing and chunk spans multiple pages → binary-split
         and recurse. (Single-page chunks just return empty + log.)
    """
    chunk_text = "\n\n".join(text for _, text in chunk)
    normalized_chunk = " ".join(chunk_text.split())
    page_range = (
        f"{chunk[0][0]}-{chunk[-1][0]}" if len(chunk) > 1 else str(chunk[0][0])
    )

    if spec is None:
        spec = load_specs()[knowledge_type]
    system_prompt = render_segmentation_prompt(spec)
    user_msg = _build_user_message(
        file_name, page_range, project_hint, equipment_hint, chunk_text,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    try:
        raw_response = await _call_llm(settings, messages)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "LLM call failed for pages %s: %s: %s",
            page_range, type(exc).__name__, exc,
        )
        return await _binary_split_recover(
            settings, chunk, knowledge_type, file_name,
            project_hint, equipment_hint, chunk_chars,
            spec=spec,
            reason=f"call failure: {exc}",
        )

    try:
        entries = _parse_json_array(raw_response)
        if entries:
            return [(e, normalized_chunk) for e in entries]
        log.warning("LLM returned empty entries for pages %s", page_range)
        return []
    except json.JSONDecodeError as exc:
        log.warning(
            "Invalid JSON from LLM for pages %s (%s) — attempting recovery",
            page_range, exc,
        )

    # Layer 2: repair-prompt retry, but only once per chunk.
    if not repair_attempt:
        repaired = await _try_repair_json(
            settings, messages, raw_response,
        )
        if repaired:
            return [(e, normalized_chunk) for e in repaired]

    # Layer 3: binary-split recursion.
    return await _binary_split_recover(
        settings, chunk, knowledge_type, file_name,
        project_hint, equipment_hint, chunk_chars,
        spec=spec,
        reason="json parse failure after repair",
    )


def _build_user_message(
    file_name: str,
    page_range: str,
    project_hint: str | None,
    equipment_hint: str | None,
    chunk_text: str,
) -> str:
    parts = [f'Text extracted from file "{file_name}", pages {page_range}.']
    if project_hint:
        parts.append(f"Project: {project_hint}")
    if equipment_hint:
        parts.append(f"Equipment: {equipment_hint}")
    parts.append(f"\nExtract all entries:\n\n---TEXT START---\n{chunk_text}\n---TEXT END---")
    return "\n".join(parts)


async def _try_repair_json(
    settings: Settings,
    original_messages: list[dict[str, str]],
    bad_response: str,
) -> list[dict[str, Any]]:
    """One-shot repair: show the LLM its own bad output and ask for fix."""
    repair_messages = original_messages + [
        {"role": "assistant", "content": bad_response[:4000]},
        {
            "role": "user",
            "content": (
                "Your previous response was not valid JSON. "
                "Re-emit the SAME entries as a valid JSON array only. "
                "No prose, no markdown fence, no explanation. "
                "If you ran out of room, return only the complete entries you finished."
            ),
        },
    ]
    try:
        repaired_raw = await _call_llm(settings, repair_messages)
        return _parse_json_array(repaired_raw)
    except (json.JSONDecodeError, Exception) as exc:  # noqa: BLE001
        log.warning("Repair attempt failed: %s", exc)
        return []


async def _binary_split_recover(
    settings: Settings,
    chunk: list[PageText],
    knowledge_type: KnowledgeType,
    file_name: str,
    project_hint: str | None,
    equipment_hint: str | None,
    chunk_chars: int,
    *,
    spec: TypeSpec | None = None,
    reason: str,
) -> list[tuple[dict[str, Any], str]]:
    """Split a failed chunk on a page boundary and re-segment each half.

    Floor at _MIN_SUBCHUNK_PAGES — if we're already there, give up cleanly.
    A 1-page chunk that's still too large for the model has already been
    structurally subdivided in chunk_pages, so we'd just thrash.
    """
    if len(chunk) <= _MIN_SUBCHUNK_PAGES:
        page_range = str(chunk[0][0]) if chunk else "?"
        log.error(
            "Giving up on chunk page %s (%s) — single-page chunk could not be parsed",
            page_range, reason,
        )
        return []

    mid = len(chunk) // 2
    first_half = chunk[:mid]
    second_half = chunk[mid:]
    log.info(
        "Binary-split recovery: pages %d-%d → [%d-%d] + [%d-%d] (%s)",
        chunk[0][0], chunk[-1][0],
        first_half[0][0], first_half[-1][0],
        second_half[0][0], second_half[-1][0],
        reason,
    )
    out: list[tuple[dict[str, Any], str]] = []
    for sub in (first_half, second_half):
        out.extend(
            await _segment_chunk_with_fallback(
                settings, sub, knowledge_type, file_name,
                project_hint, equipment_hint, chunk_chars,
                spec=spec,
                # repair_attempt=True suppresses a second repair-prompt on the
                # already-narrower sub-chunks; binary split is the better tool
                # at this depth.
                repair_attempt=True,
            )
        )
    return out


# ── Deduplication ────────────────────────────────────────────────────────────

def _deduplicate_entries(
    entries: list[tuple[dict[str, Any], str]],
    knowledge_type: KnowledgeType,
) -> list[tuple[dict[str, Any], str]]:
    """Dedupe entries duplicated across overlapping chunks.

    Per-type key:
      - ALARM:      normalized error_code
      - SETUP:      normalized station + first 80 chars of procedure
      - EXPERIENCE: normalized problem + first 80 chars of failure_desc

    Same key with higher confidence wins. Entries with empty keys pass through
    untouched (preserved as distinct items) — better to over-keep and let the
    human reviewer reject than silently drop borderline data.
    """
    if knowledge_type == KnowledgeType.ALARM:
        return _deduplicate_alarms_with_context(entries)
    if knowledge_type == KnowledgeType.SETUP:
        return _dedupe_by_key(
            entries,
            lambda e: _norm_key(e.get("station", ""), e.get("procedure", "")),
        )
    if knowledge_type == KnowledgeType.EXPERIENCE:
        return _dedupe_by_key(
            entries,
            lambda e: _norm_key(e.get("problem", ""), e.get("failure_desc", "")),
        )
    return entries


def _norm_key(*parts: str, length: int = 80) -> str:
    """Build a normalization-tolerant dedup key from text fragments."""
    pieces = []
    for p in parts:
        if not p:
            continue
        collapsed = " ".join(str(p).split())
        pieces.append(collapsed[:length].lower())
    return "|".join(pieces)


def _dedupe_by_key(
    entries: list[tuple[dict[str, Any], str]],
    key_fn: Callable[[dict[str, Any]], str],
) -> list[tuple[dict[str, Any], str]]:
    seen: dict[str, tuple[dict[str, Any], str]] = {}
    extras: list[tuple[dict[str, Any], str]] = []
    for entry, chunk_text in entries:
        key = key_fn(entry)
        if not key:
            extras.append((entry, chunk_text))
            continue
        if key in seen:
            existing_entry, _ = seen[key]
            if entry.get("confidence", 0) > existing_entry.get("confidence", 0):
                seen[key] = (entry, chunk_text)
        else:
            seen[key] = (entry, chunk_text)
    return list(seen.values()) + extras


def _deduplicate_alarms_with_context(
    entries: list[tuple[dict[str, Any], str]],
) -> list[tuple[dict[str, Any], str]]:
    """Deduplicate alarm entries from overlapping chunks by error_code, keeping higher-confidence."""
    seen: dict[str, tuple[dict[str, Any], str]] = {}
    for entry, chunk_text in entries:
        code = entry.get("error_code", "").strip().upper()
        if not code:
            seen[f"_unknown_{len(seen)}"] = (entry, chunk_text)
            continue
        if code in seen:
            existing_entry, _ = seen[code]
            if entry.get("confidence", 0) > existing_entry.get("confidence", 0):
                seen[code] = (entry, chunk_text)
        else:
            seen[code] = (entry, chunk_text)
    return list(seen.values())


# ── Staged-doc conversion ────────────────────────────────────────────────────

def _parsed_to_staged(
    idx: int,
    entry: dict[str, Any],
    knowledge_type: KnowledgeType,
    file_name: str,
    project_hint: str | None,
    equipment_hint: str | None,
    normalized_chunk_text: str,
    normalized_full_raw: str,
) -> StagedDocument:
    """Convert LLM-parsed entry to a StagedDocument."""
    source_pages = [str(p) for p in entry.get("source_pages", [])]
    try:
        confidence = float(entry.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    warnings: list[str] = []

    doc = StagedDocument(
        index=idx,
        knowledge_type=knowledge_type,
        project=project_hint or "",
        equipment=equipment_hint or "",
        source_file=file_name,
        source_pages=source_pages,
        confidence=confidence,
        warnings=warnings,
    )

    if knowledge_type == KnowledgeType.ALARM:
        error_code = entry.get("error_code", "").strip().upper()
        title_zh = entry.get("title_zh", "") or ""
        title_en = entry.get("title_en", "") or ""
        if title_zh and title_en:
            doc.title = f"{title_zh}（{title_en}）"[:200]
        elif title_zh:
            doc.title = title_zh[:200]
        elif title_en:
            doc.title = title_en[:200]
        else:
            doc.title = (entry.get("title", "") or "")[:200]
        doc.error_codes = [error_code] if error_code else []
        doc.content = entry.get("content", "—") or "—"
        doc.resolution = entry.get("resolution", "—") or "—"
        doc.notes = entry.get("notes", "") or ""

        for field_name in ("content", "resolution"):
            val = getattr(doc, field_name)
            if val and val != "—":
                if not _fidelity_ok(val, normalized_chunk_text, normalized_full_raw):
                    warnings.append(f"fabrication_warning: {field_name}")

    elif knowledge_type == KnowledgeType.SETUP:
        doc.title = (entry.get("station", "") or "")[:200]
        doc.prerequisites = entry.get("prerequisites", "") or ""
        doc.procedure = entry.get("procedure", "—") or "—"
        doc.notes = entry.get("notes", "") or ""

        if doc.procedure and doc.procedure != "—":
            if not _fidelity_ok(doc.procedure, normalized_chunk_text, normalized_full_raw):
                warnings.append("fabrication_warning: procedure")

    elif knowledge_type == KnowledgeType.EXPERIENCE:
        doc.title = (entry.get("problem", "") or "")[:200]
        failure_desc = entry.get("failure_desc", "") or ""
        analysis = entry.get("analysis", "") or ""
        root_cause = entry.get("root_cause", "") or ""
        parts: list[str] = []
        if failure_desc:
            parts.append(failure_desc)
        if analysis:
            parts.append(f"【失败分析】{analysis}")
        if root_cause:
            parts.append(f"【根因】{root_cause}")
        doc.body_text = "\n\n".join(parts) if parts else "—"
        doc.procedure = entry.get("procedure", "") or ""
        doc.notes = entry.get("notes", "") or ""

        if (
            doc.body_text and doc.body_text != "—" and failure_desc
            and not _fidelity_ok(failure_desc, normalized_chunk_text, normalized_full_raw)
        ):
            warnings.append("fabrication_warning: failure_desc")

    doc.raw_text_excerpt = _build_excerpt(entry, knowledge_type)
    doc.warnings = warnings
    return doc


def _fidelity_ok(value: str, normalized_chunk: str, normalized_full_raw: str) -> bool:
    """Check fidelity against chunk text first; fall back to full-file text."""
    return (
        verify_extraction_fidelity(value, normalized_chunk, pre_normalized=True)
        or verify_extraction_fidelity(value, normalized_full_raw, pre_normalized=True)
    )


def _build_excerpt(entry: dict[str, Any], kt: KnowledgeType) -> str:
    """Build a raw text excerpt from the parsed entry for audit."""
    parts: list[str] = []
    if kt == KnowledgeType.ALARM:
        for k in ("error_code", "title", "title_zh", "content", "resolution"):
            v = entry.get(k, "")
            if v:
                parts.append(f"{k}: {str(v)[:200]}")
    elif kt == KnowledgeType.SETUP:
        for k in ("station", "procedure"):
            v = entry.get(k, "")
            if v:
                parts.append(f"{k}: {str(v)[:200]}")
    else:
        for k in ("problem", "failure_desc", "root_cause"):
            v = entry.get(k, "")
            if v:
                parts.append(f"{k}: {str(v)[:200]}")
    return "\n".join(parts)[:500]


# ── Anti-fabrication ─────────────────────────────────────────────────────────

def verify_extraction_fidelity(
    structured_field: str,
    raw_text: str,
    threshold: float = 0.6,
    *,
    pre_normalized: bool = False,
) -> bool:
    """Check that the structured field text actually appears in the raw extraction."""
    if pre_normalized:
        normalized_field = " ".join(structured_field.split())
        normalized_raw = raw_text
    else:
        normalized_field = " ".join(structured_field.split())
        normalized_raw = " ".join(raw_text.split())

    if len(normalized_field) < 200:
        return normalized_field in normalized_raw

    split_text = normalized_field.replace("。", "\n").replace(". ", "\n")
    sentences = [s.strip() for s in split_text.split("\n") if s.strip()]
    if not sentences:
        return True
    overlap = sum(1 for s in sentences if s in normalized_raw)
    return (overlap / len(sentences)) >= threshold
