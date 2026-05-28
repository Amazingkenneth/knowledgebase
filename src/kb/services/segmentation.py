"""LLM-based document segmentation — splits extracted text into structured documents.

The LLM acts as a structuring/parsing tool ONLY. It identifies boundaries
between alarm codes, setup procedures, or experience entries and maps each
segment to the document model fields. It must copy text verbatim — never
paraphrase or fabricate content.
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

# Rough token estimate: 1 CJK char ~ 1.5 tokens, 1 latin word ~ 1.3 tokens
_OVERLAP_PAGES = 1


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

If you cannot confidently assign text to an alarm entry, set confidence < 0.5.
Return ONLY the JSON array — no other text."""

_SETUP_SYSTEM_PROMPT = """\
You are a setup/commissioning document parser. Split the extracted text into individual setup entries.
你也可以处理中文文档。

Rules / 规则:
1. Copy text verbatim — never add, rephrase, or fabricate content.
2. Each entry: station/component name (station), prerequisites/specs (prerequisites), procedure steps (procedure).
3. Use empty string for absent fields.
4. "notes" is optional.

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

Return ONLY the JSON array — no other text."""

_EXPERIENCE_SYSTEM_PROMPT = """\
You are a failure-case / maintenance-experience document parser. Split the extracted text into individual case entries.
你也可以处理中文文档。

Rules / 规则:
1. Copy text verbatim — never add, rephrase, or fabricate content.
2. Each entry: problem title (problem), failure description (failure_desc), analysis (analysis), root cause (root_cause), corrective steps (procedure).
3. Use empty string for absent fields.

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

Return ONLY the JSON array — no other text."""

_SYSTEM_PROMPTS = {
    KnowledgeType.ALARM: _ALARM_SYSTEM_PROMPT,
    KnowledgeType.SETUP: _SETUP_SYSTEM_PROMPT,
    KnowledgeType.EXPERIENCE: _EXPERIENCE_SYSTEM_PROMPT,
}

# When knowledge_type is unknown, use this prompt to auto-detect
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

def _estimate_timeout(messages: list[dict[str, str]], max_tokens: int) -> float:
    """Derive a read-timeout from the actual message payload.

    1 input char ≈ 0.5 token (CJK-aware); expected output ≈ 25% of input tokens
    (structured JSON extraction); Qwen throughput ≈ 80 tok/s; 20 s base overhead.
    Minimum is 60 s to absorb cold-start latency.
    """
    total_input_chars = sum(len(m["content"]) for m in messages)
    input_tokens = total_input_chars / 2
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


# ── Chunking ─────────────────────────────────────────────────────────────────

def chunk_pages(pages: list[PageText], max_chars: int = 12000) -> list[list[PageText]]:
    """Split pages into chunks that fit within max_chars, with 1-page overlap."""
    if not pages:
        return []
    chunks: list[list[PageText]] = []
    current: list[PageText] = []
    current_len = 0

    for page in pages:
        page_len = len(page[1])
        if current and current_len + page_len > max_chars:
            chunks.append(current)
            # overlap: keep last page
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
) -> KnowledgeType:
    """Use LLM to detect the document type from a text sample."""
    messages = [
        {"role": "system", "content": _DETECT_TYPE_PROMPT},
        {"role": "user", "content": sample_text[:2000]},
    ]
    try:
        raw = await _call_llm(settings, messages, max_tokens=50)
        parsed = json.loads(_strip_code_fence(raw))
        type_str = parsed.get("type", "")
        return KnowledgeType(type_str)
    except Exception as exc:
        log.warning("Could not auto-detect document type: %s — defaulting to experience", exc)
        return KnowledgeType.EXPERIENCE


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

    system_prompt = _SYSTEM_PROMPTS[knowledge_type]
    chunk_chars = settings.ingest.segmentation_chunk_chars
    chunks = chunk_pages(pages, max_chars=chunk_chars)

    # Pre-normalize once — avoids O(docs × fields) re-normalization inside fidelity checks
    full_raw_text = "\n\n".join(text for _, text in pages)
    normalized_full_raw = " ".join(full_raw_text.split())

    all_parsed: list[tuple[dict[str, Any], str]] = []  # (entry, normalized_chunk_text)

    for chunk_idx, chunk in enumerate(chunks):
        if on_chunk_progress:
            on_chunk_progress(chunk_idx + 1, len(chunks))
        page_range = f"{chunk[0][0]}-{chunk[-1][0]}" if len(chunk) > 1 else str(chunk[0][0])
        chunk_text = "\n\n".join(text for _, text in chunk)
        normalized_chunk = " ".join(chunk_text.split())

        user_msg = f'Text extracted from file "{file_name}", pages {page_range}.\n'
        if project_hint:
            user_msg += f"Project: {project_hint}\n"
        if equipment_hint:
            user_msg += f"Equipment: {equipment_hint}\n"
        user_msg += f"\nExtract all entries:\n\n---TEXT START---\n{chunk_text}\n---TEXT END---"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

        try:
            raw_response = await _call_llm(settings, messages)
            cleaned = _sanitize_json(_strip_code_fence(raw_response))
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                all_parsed.extend((e, normalized_chunk) for e in parsed)
            elif isinstance(parsed, dict):
                all_parsed.append((parsed, normalized_chunk))
        except json.JSONDecodeError as exc:
            log.warning("LLM returned invalid JSON for chunk pages %s: %s", page_range, exc)
        except Exception as exc:
            log.warning(
                "Segmentation failed for chunk pages %s: %s: %s",
                page_range, type(exc).__name__, exc,
                exc_info=True,
            )

    # Deduplicate by error_code (for overlapping alarm chunks)
    if knowledge_type == KnowledgeType.ALARM:
        # Unwrap, deduplicate on entries, re-associate chunk text by code
        raw_entries = [(e, ct) for e, ct in all_parsed]
        deduped = _deduplicate_alarms_with_context(raw_entries)
        all_parsed = deduped

    # Convert to StagedDocuments — fidelity checked against chunk text first,
    # then full text as fallback (handles content that spans a chunk boundary)
    docs: list[StagedDocument] = []
    for idx, (entry, normalized_chunk) in enumerate(all_parsed):
        doc = _parsed_to_staged(
            idx, entry, knowledge_type, file_name,
            project_hint, equipment_hint,
            normalized_chunk, normalized_full_raw,
        )
        docs.append(doc)

    return docs


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
            existing_entry, existing_chunk = seen[code]
            if entry.get("confidence", 0) > existing_entry.get("confidence", 0):
                seen[code] = (entry, chunk_text)
        else:
            seen[code] = (entry, chunk_text)
    return list(seen.values())


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
    """Convert LLM-parsed entry to a StagedDocument.

    normalized_chunk_text: pre-normalized text of the chunk that produced this entry.
    normalized_full_raw: pre-normalized text of the entire file (fidelity fallback).
    Both are already whitespace-collapsed so verify_extraction_fidelity skips re-normalization.
    """
    source_pages = [str(p) for p in entry.get("source_pages", [])]
    confidence = float(entry.get("confidence", 0.5))
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
        title_zh = entry.get("title_zh", "")
        title_en = entry.get("title_en", "")
        if title_zh and title_en:
            doc.title = f"{title_zh}（{title_en}）"[:200]
        elif title_zh:
            doc.title = title_zh[:200]
        elif title_en:
            doc.title = title_en[:200]
        else:
            # English-only or generic title field
            doc.title = entry.get("title", "")[:200]
        doc.error_codes = [error_code] if error_code else []
        doc.content = entry.get("content", "—")
        doc.resolution = entry.get("resolution", "—")
        doc.notes = entry.get("notes", "")

        # Fabrication check: chunk text first (strict), fall back to full file text
        for field_name in ("content", "resolution"):
            val = getattr(doc, field_name)
            if val and val != "—":
                if not _fidelity_ok(val, normalized_chunk_text, normalized_full_raw):
                    warnings.append(f"fabrication_warning: {field_name}")

    elif knowledge_type == KnowledgeType.SETUP:
        doc.title = entry.get("station", "")[:200]
        doc.prerequisites = entry.get("prerequisites", "")
        doc.procedure = entry.get("procedure", "—")
        doc.notes = entry.get("notes", "")

        if doc.procedure and doc.procedure != "—":
            if not _fidelity_ok(doc.procedure, normalized_chunk_text, normalized_full_raw):
                warnings.append("fabrication_warning: procedure")

    elif knowledge_type == KnowledgeType.EXPERIENCE:
        doc.title = entry.get("problem", "")[:200]
        failure_desc = entry.get("failure_desc", "")
        analysis = entry.get("analysis", "")
        root_cause = entry.get("root_cause", "")
        parts = []
        if failure_desc:
            parts.append(failure_desc)
        if analysis:
            parts.append(f"【失败分析】{analysis}")
        if root_cause:
            parts.append(f"【根因】{root_cause}")
        doc.body_text = "\n\n".join(parts) if parts else "—"
        doc.procedure = entry.get("procedure", "")
        doc.notes = entry.get("notes", "")

    # Build raw_text_excerpt for preview
    doc.raw_text_excerpt = _build_excerpt(entry, knowledge_type)
    doc.warnings = warnings
    return doc


def _fidelity_ok(value: str, normalized_chunk: str, normalized_full_raw: str) -> bool:
    """Check fidelity against chunk text first; fall back to full-file text.

    Both inputs are already whitespace-normalized, avoiding re-normalization cost.
    Content split across a chunk boundary will pass via the full-file fallback.
    """
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
                parts.append(f"{k}: {v[:200]}")
    elif kt == KnowledgeType.SETUP:
        for k in ("station", "procedure"):
            v = entry.get(k, "")
            if v:
                parts.append(f"{k}: {v[:200]}")
    else:
        for k in ("problem", "failure_desc", "root_cause"):
            v = entry.get(k, "")
            if v:
                parts.append(f"{k}: {v[:200]}")
    return "\n".join(parts)[:500]


# ── Anti-fabrication ─────────────────────────────────────────────────────────

def verify_extraction_fidelity(
    structured_field: str,
    raw_text: str,
    threshold: float = 0.6,
    *,
    pre_normalized: bool = False,
) -> bool:
    """Check that the structured field text actually appears in the raw extraction.

    Set pre_normalized=True when both inputs are already whitespace-collapsed to skip
    the expensive re-normalization step.
    """
    if pre_normalized:
        normalized_field = " ".join(structured_field.split())
        normalized_raw = raw_text
    else:
        normalized_field = " ".join(structured_field.split())
        normalized_raw = " ".join(raw_text.split())

    if len(normalized_field) < 200:
        return normalized_field in normalized_raw

    # For longer texts, check sentence-level overlap
    # Split on Chinese period, regular period, or newlines
    split_text = normalized_field.replace("。", "\n").replace(". ", "\n")
    sentences = [s.strip() for s in split_text.split("\n") if s.strip()]
    if not sentences:
        return True
    overlap = sum(1 for s in sentences if s in normalized_raw)
    return (overlap / len(sentences)) >= threshold
