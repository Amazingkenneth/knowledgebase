"""LLM proxy endpoints.

POST /api/v1/chat    — Forward a conversation to the configured LLM. The
                       frontend no longer needs a hardcoded API key.

POST /api/v1/extract — Extract structured search parameters from a free-text
                       query using the LLM primed with the live taxonomy.
                       Falls back gracefully when no API key is configured.
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from kb.api.deps import SettingsDep, TaxonomyDep

log = logging.getLogger("kb.chat")

router = APIRouter(prefix="/api/v1", tags=["chat"])


# ── Shared helpers ────────────────────────────────────────────────────────────

class _Message(BaseModel):
    role: str
    content: str


async def _call_llm(settings, messages: list[dict], timeout: float = 20.0) -> str:
    """POST to the configured OpenAI-compat chat endpoint. Returns assistant text."""
    if not settings.llm.api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM not configured — set KB_LLM__API_KEY environment variable.",
        )
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            settings.llm.api_url,
            headers={"Authorization": f"Bearer {settings.llm.api_key}"},
            json={
                "model": settings.llm.model,
                "messages": messages,
                "max_tokens": settings.llm.max_tokens,
                "stream": False,
            },
        )
    if resp.status_code != 200:
        log.warning("LLM upstream error %s: %s", resp.status_code, resp.text[:200])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM upstream returned {resp.status_code}",
        )
    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ── /chat ─────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    messages: list[_Message]
    system: str | None = None


class ChatResponse(BaseModel):
    content: str


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, settings: SettingsDep) -> ChatResponse:
    """Proxy a conversation to the configured LLM. The API key stays server-side."""
    msgs: list[dict] = []
    if body.system:
        msgs.append({"role": "system", "content": body.system})
    msgs.extend(m.model_dump() for m in body.messages)
    content = await _call_llm(settings, msgs)
    return ChatResponse(content=content)


# ── /extract ──────────────────────────────────────────────────────────────────

class ExtractRequest(BaseModel):
    query: str


class ExtractResponse(BaseModel):
    project: str | None = None
    knowledge_type: str | None = None
    error_codes: list[str] = []
    equipment: str | None = None
    keywords: list[str] = []
    is_sentence: bool = False


def _build_extract_system(taxonomy) -> str:
    projects = ", ".join(taxonomy.projects)
    equipment = ", ".join(taxonomy.equipment)
    return f"""你是一个制造业知识库的参数提取助手。
从用户的查询中提取以下结构化参数，并以 JSON 格式返回，不要有任何其他文字。

可用的枚举值（只能选择列表中的值，否则返回 null）：
  项目 (project): {projects}
  机台/设备 (equipment): {equipment}
  知识类型 (knowledge_type): alarm（机台报警）, setup（机台调试/setup）, experience（设备经验/故障案例）

返回格式（JSON，不含任何解释）：
{{
  "project": <项目名或 null>,
  "knowledge_type": <"alarm"|"setup"|"experience" 或 null>,
  "error_codes": [<报警代码字符串列表，如 ["125002"]，没有则为空数组>],
  "equipment": <机台/设备名或 null>,
  "keywords": [<3-5 个检索关键词，不含项目名和机台名>],
  "is_sentence": <true 如果是自然语言问句，false 如果是关键词组合>
}}"""


@router.post("/extract", response_model=ExtractResponse)
async def extract_params(
    body: ExtractRequest,
    settings: SettingsDep,
    taxonomy_store: TaxonomyDep,
) -> ExtractResponse:
    """Use the LLM to extract structured search parameters from a free-text query.

    The system prompt is built from the live taxonomy so project/equipment names
    are always current. Returns 503 when no API key is configured so the frontend
    can fall back to its rule-based parser silently.
    """
    taxonomy = taxonomy_store.current
    system = _build_extract_system(taxonomy)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": body.query},
    ]
    raw = await _call_llm(settings, messages, timeout=8.0)
    try:
        # Strip markdown code fences if the model wraps the JSON
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        parsed = json.loads(text)
        return ExtractResponse(
            project=parsed.get("project"),
            knowledge_type=parsed.get("knowledge_type"),
            error_codes=parsed.get("error_codes") or [],
            equipment=parsed.get("equipment"),
            keywords=parsed.get("keywords") or [],
            is_sentence=bool(parsed.get("is_sentence", False)),
        )
    except Exception as exc:
        log.warning("extract: failed to parse LLM JSON — %s | raw=%s", exc, raw[:200])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM returned unparseable response",
        )
