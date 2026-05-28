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
from kb.models.ingest import StagedDocument
from kb.models.taxonomy import KnowledgeType
from kb.services.extraction import PageText

log = logging.getLogger("kb.segmentation")

_OVERLAP_PAGES = 1
_MIN_SUBCHUNK_PAGES = 1  # recursion floor for binary-split fallback


# ── System prompts ───────────────────────────────────────────────────────────

_ALARM_SYSTEM_PROMPT = """\
You are an alarm-code document parser. Split the extracted text into individual alarm entries.
你也可以处理中文文档。

Rules / 规则:
1. Copy text verbatim from the source — never add, rephrase, or fabricate content.
   只使用原文，不要添加或改写任何内容。
2. Every entry must have: error_code, title, content, resolution. Use "—" when a field is absent in the source.
3. "notes" is optional (parameters, continuation instructions, warnings).
4. Preserve original formatting and numbering.
5. Lines like "| col | col | col |" are table rows — treat each row as one logical record when it carries a complete entry.

Output a JSON array. Each element:
{
  "error_code": "1030",        // alarm/fault number exactly as it appears (e.g. "1030", "F7011", "E1001")
  "title": "Full alarm title from the source text",
  "title_zh": "",              // Chinese title if present, else empty string
  "title_en": "",              // English title if present, else empty string
  "content": "Definitions and Reaction sections copied verbatim",
  "resolution": "Remedy / 解除流程 section copied verbatim",
  "notes": "Parameters and Program Continuation sections if present, else empty string",
  "source_pages": [14, 15],
  "confidence": 0.85
}

confidence scoring:
- 0.9–1.0: entry is clearly delimited with an explicit alarm code and complete fields
- 0.6–0.9: entry boundaries are reasonably clear but some fields are inferred
- 0.3–0.6: ambiguous boundary or missing key fields
- 0.0–0.3: cannot confidently assign text to a single alarm entry

Alarm entry boundaries — look for:
- A standalone numeric or alphanumeric code on its own line (e.g. "1030", "F7011", "E1001", "700001")
- A new section heading or chapter title
- A visible separator line or clear topic change
- A table row whose first column matches an alarm code pattern

If you cannot confidently assign text to an alarm entry, set confidence < 0.5.
Return ONLY the JSON array — no other text, no markdown fence."""

_SETUP_SYSTEM_PROMPT = """\
You are a setup/commissioning document parser. Split the extracted text into individual setup entries.
你也可以处理中文文档。

Rules / 规则:
1. Copy text verbatim — never add, rephrase, or fabricate content.
2. Each entry: station/component name (station), prerequisites/specs (prerequisites), procedure steps (procedure).
3. Use empty string for absent fields.
4. "notes" is optional.
5. Lines like "| col | col | col |" are table rows — preserve the cell order when copying.

Output a JSON array:
{
  "station": "Station or component name",
  "prerequisites": "Specifications/requirements copied verbatim",
  "procedure": "Setup steps copied verbatim",
  "notes": "Warnings or additional notes if present",
  "source_pages": [1, 2],
  "confidence": 0.85
}

confidence scoring:
- 0.9–1.0: station/component is clearly named and procedure steps are complete
- 0.6–0.9: entry is identifiable but some fields are inferred or sparse
- 0.0–0.6: entry boundaries are ambiguous or most fields are missing

Entry boundaries — look for:
- New station or component name
- New chapter heading or numbered section
- Procedure steps restarting from 1

Return ONLY the JSON array — no other text, no markdown fence."""

_EXPERIENCE_SYSTEM_PROMPT = """\
You are a failure-case / maintenance-experience document parser. Split the extracted text into individual case entries.
你也可以处理中文文档。

Rules / 规则:
1. Copy text verbatim — never add, rephrase, or fabricate content.
2. Each entry: problem title (problem), failure description (failure_desc), analysis (analysis), root cause (root_cause), corrective steps (procedure).
3. Use empty string for absent fields.
4. Lines like "| col | col | col |" are table rows — preserve them verbatim.

Output a JSON array:
{
  "problem": "Problem title or case heading",
  "failure_desc": "Failure description copied verbatim",
  "analysis": "Failure analysis copied verbatim",
  "root_cause": "Root cause copied verbatim",
  "procedure": "Corrective steps copied verbatim",
  "notes": "",
  "source_pages": [3],
  "confidence": 0.8
}

confidence scoring:
- 0.9–1.0: case has a clear title, full failure description, root cause, and corrective steps
- 0.6–0.9: case is identifiable but some sections are incomplete
- 0.0–0.6: ambiguous case boundaries or most fields are absent

Entry boundaries — look for:
- New problem title or case number
- Keywords: Problem/故障/案例/Failure/Issue/Case
- Clear topic change

Return ONLY the JSON array — no other text, no markdown fence."""

_SYSTEM_PROMPTS = {
    KnowledgeType.ALARM: _ALARM_SYSTEM_PROMPT,
    KnowledgeType.SETUP: _SETUP_SYSTEM_PROMPT,
    KnowledgeType.EXPERIENCE: _EXPERIENCE_SYSTEM_PROMPT,
}

_DETECT_TYPE_PROMPT = """\
Analyze the following text and classify the document type.
分析以下文本，判断文档类型。

Types:
1. "alarm" — alarm/fault codes with numeric or alphanumeric IDs (e.g. 1030, F7011, E1001), descriptions, and remedy steps.
         报警代码文档，包含报警编号、描述、解除流程。
2. "setup" — commissioning or setup procedures with station names, specifications, and numbered steps.
         调试文档，包含工站名称、规格、步骤。
3. "experience" — failure cases or maintenance experience with problem description, analysis, root cause, and corrective steps.
         故障案例/经验文档，包含问题描述、分析、根因、纠正步骤。

Return ONLY JSON: {"type": "alarm"} or {"type": "setup"} or {"type": "experience"}
只返回 JSON，不要其他文本。"""


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

async def detect_knowledge_type(
    settings: Settings,
    sample_text: str,
    pages: list[PageText] | None = None,
) -> KnowledgeType:
    """Use LLM to detect the document type.

    If `pages` is given, build a richer sample by stitching head/middle/tail
    excerpts — covers and TOCs at the start can mislead a head-only sample.
    Falls back to `sample_text[:2000]` when pages are not available.
    """
    sample = (
        _build_type_detection_sample(pages, char_budget=2400)
        if pages else sample_text[:2000]
    )

    messages = [
        {"role": "system", "content": _DETECT_TYPE_PROMPT},
        {"role": "user", "content": sample},
    ]
    try:
        raw = await _call_llm(settings, messages, max_tokens=50)
        parsed = json.loads(_strip_code_fence(raw))
        type_str = parsed.get("type", "")
        return KnowledgeType(type_str)
    except Exception as exc:  # noqa: BLE001 — best-effort classification
        log.warning("Could not auto-detect document type: %s — defaulting to experience", exc)
        return KnowledgeType.EXPERIENCE


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
    knowledge_type: KnowledgeType,
    file_name: str,
    project_hint: str | None = None,
    equipment_hint: str | None = None,
    on_chunk_progress: Callable[[int, int], None] | None = None,
) -> list[StagedDocument]:
    """Segment extracted text into structured documents using the LLM."""
    if not pages:
        return []

    chunk_chars = settings.ingest.segmentation_chunk_chars
    chunks = chunk_pages(pages, max_chars=chunk_chars)

    full_raw_text = "\n\n".join(text for _, text in pages)
    normalized_full_raw = " ".join(full_raw_text.split())

    all_parsed: list[tuple[dict[str, Any], str]] = []  # (entry, normalized_chunk_text)
    total_chunks = len(chunks)

    for chunk_idx, chunk in enumerate(chunks):
        if on_chunk_progress:
            on_chunk_progress(chunk_idx + 1, total_chunks)
        entries = await _segment_chunk_with_fallback(
            settings, chunk, knowledge_type, file_name,
            project_hint, equipment_hint,
            chunk_chars=chunk_chars,
        )
        all_parsed.extend(entries)

    deduped = _deduplicate_entries(all_parsed, knowledge_type)

    docs: list[StagedDocument] = []
    for idx, (entry, normalized_chunk) in enumerate(deduped):
        doc = _parsed_to_staged(
            idx, entry, knowledge_type, file_name,
            project_hint, equipment_hint,
            normalized_chunk, normalized_full_raw,
        )
        docs.append(doc)

    return docs


async def _segment_chunk_with_fallback(
    settings: Settings,
    chunk: list[PageText],
    knowledge_type: KnowledgeType,
    file_name: str,
    project_hint: str | None,
    equipment_hint: str | None,
    chunk_chars: int,
    *,
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

    system_prompt = _SYSTEM_PROMPTS[knowledge_type]
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
