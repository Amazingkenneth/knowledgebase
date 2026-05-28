"""Knowledge-type specs — single source of truth for LLM segmentation prompts.

Each `config/knowledge_types/<type>.yaml` describes the fields, boundary
hints, skip rules, and a worked example for one knowledge type. This module
loads the YAMLs and renders them into:

  1. a per-type system prompt for the segmentation LLM
  2. a routing prompt for per-chunk type classification (alarm / setup /
     experience / skip)

Keeping the spec in YAML — not Python string literals — means non-developers
can extend or correct field definitions without editing code, and the
prompt the LLM sees is always in sync with the contract humans read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from kb.models.taxonomy import KnowledgeType


@dataclass(frozen=True)
class FieldSpec:
    name: str
    desc: str
    label_zh: str = ""
    csv_column: str = ""
    required: bool = False


@dataclass(frozen=True)
class TypeSpec:
    type: KnowledgeType
    display_name: str
    summary_zh: str
    summary_en: str
    csv_source: str
    fields: tuple[FieldSpec, ...]
    boundary_hints: tuple[str, ...]
    confidence_guide: str
    skip_if: tuple[str, ...]
    example_input: str
    example_output: list[dict[str, Any]]

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)

    @property
    def summary(self) -> str:
        """Bilingual one-liner used in the router prompt."""
        bits = [s for s in (self.summary_en, self.summary_zh) if s]
        return " ".join(bits)


_DEFAULT_SPEC_DIR = Path("config/knowledge_types")


def _load_one(path: Path) -> TypeSpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        # Back-compat: accept either {summary_zh, summary_en} or a single
        # legacy `summary` field.
        summary_zh = (raw.get("summary_zh") or "").strip()
        summary_en = (raw.get("summary_en") or raw.get("summary") or "").strip()
        return TypeSpec(
            type=KnowledgeType(raw["type"]),
            display_name=raw.get("display_name", raw["type"]),
            summary_zh=summary_zh,
            summary_en=summary_en,
            csv_source=(raw.get("csv_source") or "").strip(),
            fields=tuple(
                FieldSpec(
                    name=f["name"],
                    desc=(f.get("desc") or "").strip(),
                    label_zh=(f.get("label_zh") or "").strip(),
                    csv_column=(f.get("csv_column") or "").strip(),
                    required=bool(f.get("required", False)),
                )
                for f in raw.get("fields", [])
            ),
            boundary_hints=tuple(raw.get("boundary_hints", [])),
            confidence_guide=raw.get("confidence_guide", "").strip(),
            skip_if=tuple(raw.get("skip_if", [])),
            example_input=raw.get("example_input", "").strip(),
            example_output=raw.get("example_output", []),
        )
    except KeyError as exc:
        raise ValueError(f"{path}: missing required key {exc}") from exc


@lru_cache(maxsize=1)
def load_specs(spec_dir: str | Path = _DEFAULT_SPEC_DIR) -> dict[KnowledgeType, TypeSpec]:
    """Load all type specs from `spec_dir`. Cached for the process lifetime."""
    directory = Path(spec_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"Spec directory not found: {directory}")
    specs: dict[KnowledgeType, TypeSpec] = {}
    for path in sorted(directory.glob("*.yaml")):
        spec = _load_one(path)
        specs[spec.type] = spec
    missing = set(KnowledgeType) - set(specs)
    if missing:
        raise ValueError(
            f"Missing spec file(s) in {directory} for: {sorted(m.value for m in missing)}"
        )
    return specs


# ── Prompt rendering ─────────────────────────────────────────────────────────

def render_segmentation_prompt(spec: TypeSpec) -> str:
    """Render the system prompt for segmenting a chunk of one known type.

    Includes an explicit "ignore other-type content" rule so when the chunk
    is mixed (e.g. an alarm followed by a setup procedure on the same page),
    each per-type segmenter call extracts only its own type and does NOT
    coerce other-type text into a malformed entry.
    """
    field_lines = []
    for f in spec.fields:
        tag = " (required)" if f.required else ""
        zh = f" / {f.label_zh}" if f.label_zh else ""
        csv = f" [CSV col: {f.csv_column}]" if f.csv_column else ""
        field_lines.append(f'  - "{f.name}"{zh}{tag}{csv}: {f.desc}')
    example_json = json.dumps(spec.example_output, ensure_ascii=False, indent=2)

    skip_lines = "\n".join(f"  - {s}" for s in spec.skip_if)
    hint_lines = "\n".join(f"  - {h}" for h in spec.boundary_hints)

    required_names = [f.name for f in spec.fields if f.required]
    required_clause = (
        ", ".join(f'"{n}"' for n in required_names) if required_names else "none"
    )
    return f"""\
You are a {spec.display_name} document parser. Split the extracted text into individual entries.
你也可以处理中文文档。

{spec.summary_en}
{spec.summary_zh}

Rules / 规则:
1. Copy text verbatim from the source — never add, rephrase, or fabricate content.
   只使用原文，不要添加或改写任何内容。
2. Output a JSON array. Each element has these fields:
{chr(10).join(field_lines)}
3. Use empty string "" for optional fields absent in the source.
4. Table rows like "| col | col |" — treat each complete row as one logical record.
5. ONLY extract {spec.type.value} entries. If the chunk contains NO {spec.type.value}
   entries at all, return an empty array []. Do NOT emit skeleton entries with
   empty required fields ({required_clause}) just to fill the array —
   an empty array [] is the correct answer when no {spec.type.value} entries exist.
   如果该段落不包含 {spec.type.value} 类型条目，请返回空数组 []，不要输出空字段占位条目。
6. Required fields ({required_clause}) MUST be populated verbatim from the
   source for every emitted entry. If you cannot find a required field's
   value in the source, DROP the entry — do not output it.
7. If the chunk also contains other knowledge-type content, IGNORE that
   other content — do NOT force it into {spec.type.value} fields.

Entry boundaries — look for:
{hint_lines}

Confidence scoring:
{spec.confidence_guide}

Return an empty array [] if any of these apply:
{skip_lines}

Example input ({spec.csv_source or 'canonical sample'}):
---
{spec.example_input}
---

Example output:
{example_json}

Return ONLY the JSON array — no other text, no markdown fence."""


def render_router_prompt(specs: dict[KnowledgeType, TypeSpec]) -> str:
    """Render the per-chunk multi-type classification prompt.

    The router returns a JSON list of types present in the chunk. A single
    chunk that contains an alarm AND a setup procedure should return both,
    so each type's segmenter runs on the same chunk and extracts only its
    own entries (see render_segmentation_prompt rule 5).
    """
    type_lines = []
    for kt in KnowledgeType:
        spec = specs[kt]
        type_lines.append(f'- "{kt.value}" — {spec.summary.strip()}')
    return f"""\
Classify the text chunk by which knowledge types it contains.
对文本进行分类，识别其中包含哪些知识类型。

Return JSON: {{"types": ["alarm"|"setup"|"experience", ...]}}.

Rules:
- Default: return EXACTLY ONE type — the single dominant content type.
- Return multiple types ONLY when entries of multiple types BOTH appear with
  their own distinct structure (e.g. a standalone alarm-code table AND a
  separate numbered tuning procedure for a station — each with its own
  heading and its own entries).
- Negative example: an alarm whose "Remedy" / "解除流程" section contains
  numbered steps is STILL only {{"types": ["alarm"]}} — the remedy steps
  are part of the alarm entry, not a separate setup procedure.
- Negative example: a setup procedure that references alarm codes inline
  is STILL only {{"types": ["setup"]}} — references to alarms do not make
  a setup chunk an alarm chunk.
- Return {{"types": ["skip"]}} ONLY if the chunk is entirely non-content.
- 默认只返回单一主导类型；只有当不同类型的条目各自独立成块时才返回多个类型。
  报警条目中的解除步骤仍属于报警，不要额外标为 setup。

Types:
{chr(10).join(type_lines)}
- "skip" — non-content pages: cover, table of contents, preface, revision history,
  glossary, index, copyright notice, or pure prose with no concrete entries.
  非正文页面：封面、目录、前言、版本记录、索引、版权页等。

Return ONLY JSON. 只返回 JSON。"""
