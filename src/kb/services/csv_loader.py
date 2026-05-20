"""Load knowledge documents from the three canonical CSV files in config/.

Column mapping
--------------
机台报警_header.csv  →  AlarmDoc
  项目          → project
  机台          → equipment
  代码          → error_codes  (single code per row, wrapped in a list)
  中文标题       → title prefix; 英文标题 appended in parentheses when present
  内容          → content
  解除流程       → resolution
  注意事项       → notes
  ppt文件        → source_file
  ppt页面        → source_pages

机台setup_header.csv  →  SetupDoc
  项目          → project
  设备          → equipment
  工站/部件/站位  → title: "{设备} · {station} 调试"
  规格/要求      → prerequisites (first line)
  调试工具       → prerequisites (second line, if non-empty)
  调试步骤       → procedure
  注意事项       → notes
  ppt文件        → source_file
  PPT页面        → source_pages

设备经验_header.csv  →  ExperienceDoc
  项目          → project
  机台          → equipment
  问题          → title
  失败描述       → body_text (opening paragraph)
  失败分析       → body_text (appended section)
  根因          → body_text (appended section)
  纠正步骤       → procedure
  PPT文件        → source_file
  PPT页面        → source_pages
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

from kb.models.document import AlarmDoc, ExperienceDoc, KnowledgeDoc, SetupDoc

log = logging.getLogger("kb.csv_loader")

_CONFIG_DIR = Path("config")

ALARM_CSV     = _CONFIG_DIR / "机台报警_header.csv"
SETUP_CSV     = _CONFIG_DIR / "机台setup_header.csv"
EXPERIENCE_CSV = _CONFIG_DIR / "设备经验_header.csv"


def _pages(raw: str) -> list[str]:
    """Turn a raw page string (e.g. '14' or '1, 3') into a list of strings."""
    return [p.strip() for p in raw.split(",") if p.strip()] if raw.strip() else []


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _load_alarms(path: Path) -> list[AlarmDoc]:
    docs: list[AlarmDoc] = []
    for i, row in enumerate(_read_csv(path)):
        zh = row.get("中文标题", "").strip()
        en = row.get("英文标题", "").strip()
        title = f"{zh}（{en}）" if zh and en else (zh or en or f"Alarm {row.get('代码', i)}")
        title = title[:200]

        code_raw = row.get("代码", "").strip()
        error_codes = [c for c in re.split(r"[\s,，;&、]+", code_raw) if c] if code_raw else []

        try:
            docs.append(AlarmDoc(
                knowledge_type="alarm",
                project=row["项目"].strip(),
                equipment=row["机台"].strip(),
                error_codes=error_codes,
                title=title,
                content=row["内容"].strip() or "—",
                resolution=row["解除流程"].strip() or "—",
                notes=row.get("注意事项", "").strip(),
                source_file=row.get("ppt文件", "").strip() or None,
                source_pages=_pages(row.get("ppt页面", "")),
            ))
        except Exception as exc:
            log.warning("csv_loader: skipping alarm row %d — %s", i, exc)
    return docs


def _load_setups(path: Path) -> list[SetupDoc]:
    docs: list[SetupDoc] = []
    for i, row in enumerate(_read_csv(path)):
        equipment = row.get("设备", "").strip()
        station   = row.get("工站/部件/站位", "").strip()
        title = f"{equipment} · {station} 调试" if station else f"{equipment} 调试"
        title = title[:200]

        spec  = row.get("规格/要求", "").strip()
        tools = row.get("调试工具", "").strip()
        prerequisites_parts = []
        if spec:
            prerequisites_parts.append(f"规格/要求：{spec}")
        if tools:
            prerequisites_parts.append(f"调试工具：{tools}")
        prerequisites = "\n".join(prerequisites_parts)

        try:
            docs.append(SetupDoc(
                knowledge_type="setup",
                project=row["项目"].strip(),
                equipment=equipment,
                title=title,
                procedure=row["调试步骤"].strip() or "—",
                prerequisites=prerequisites,
                notes=row.get("注意事项", "").strip(),
                source_file=row.get("ppt文件", "").strip() or None,
                source_pages=_pages(row.get("PPT页面", "")),
            ))
        except Exception as exc:
            log.warning("csv_loader: skipping setup row %d — %s", i, exc)
    return docs


def _load_experiences(path: Path) -> list[ExperienceDoc]:
    docs: list[ExperienceDoc] = []
    for i, row in enumerate(_read_csv(path)):
        title = row.get("问题", "").strip()[:200] or f"经验 {i}"

        desc     = row.get("失败描述", "").strip()
        analysis = row.get("失败分析", "").strip()
        root     = row.get("根因", "").strip()

        body_parts = [desc] if desc else ["—"]
        if analysis:
            body_parts.append(f"【失败分析】{analysis}")
        if root:
            body_parts.append(f"【根因】{root}")
        body_text = "\n\n".join(body_parts)

        try:
            docs.append(ExperienceDoc(
                knowledge_type="experience",
                project=row["项目"].strip(),
                equipment=row["机台"].strip(),
                title=title,
                body_text=body_text,
                procedure=row.get("纠正步骤", "").strip(),
                source_file=row.get("PPT文件", "").strip() or None,
                source_pages=_pages(row.get("PPT页面", "")),
            ))
        except Exception as exc:
            log.warning("csv_loader: skipping experience row %d — %s", i, exc)
    return docs


def load_csv_documents() -> list[KnowledgeDoc]:
    """Load all documents from the three CSV files.

    Returns an empty list (with a warning) for any CSV that is missing.
    Rows that fail Pydantic validation are skipped with a warning.
    """
    docs: list[KnowledgeDoc] = []

    for path, loader, label in [
        (ALARM_CSV,       _load_alarms,       "alarm"),
        (SETUP_CSV,       _load_setups,       "setup"),
        (EXPERIENCE_CSV,  _load_experiences,  "experience"),
    ]:
        if not path.exists():
            log.warning("csv_loader: %s not found — skipping %s documents", path, label)
            continue
        loaded = loader(path)
        log.info("csv_loader: loaded %d %s documents from %s", len(loaded), label, path.name)
        docs.extend(loaded)

    return docs
