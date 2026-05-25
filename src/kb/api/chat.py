"""LLM chat endpoint with integrated knowledge-base search.

POST /api/v1/chat    — Conversational search: extracts params from the full
                       conversation, searches the KB, includes results in
                       the LLM context for contextual answering or clarification.

POST /api/v1/extract — Extract structured search parameters from a free-text
                       query using the LLM primed with the live taxonomy.
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from kb.api.deps import SearchDep, SettingsDep, TaxonomyDep
from kb.models.search import DocHit, EffectiveParams, SearchRequest, SearchStatus
from kb.models.taxonomy import KnowledgeType

log = logging.getLogger("kb.chat")

router = APIRouter(prefix="/api/v1", tags=["chat"])

_MAX_HISTORY = 20
_MAX_RESULTS_IN_CONTEXT = 6
_FULL_RESULT_THRESHOLD = 3


# ── Shared helpers ────────────────────────────────────────────────────────────

class _Message(BaseModel):
    role: str
    content: str


async def _call_llm(settings, messages: list[dict], timeout: float = 20.0) -> str:
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


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return text


# ── /chat — conversational search ───────────────────────────────────────────


class ChatRequest(BaseModel):
    messages: list[_Message]


class ChatResponse(BaseModel):
    content: str
    search_results: list[DocHit] | None = None
    search_status: SearchStatus | None = None
    effective_params: EffectiveParams | None = None


def _format_results_for_llm(hits: list[DocHit]) -> str:
    parts: list[str] = []
    for i, h in enumerate(hits[:_MAX_RESULTS_IN_CONTEXT]):
        header = (
            f"{i + 1}. 【{h.title}】项目:{h.project} 机台:{h.equipment}"
            + (f" 报警码:{','.join(h.error_codes)}" if h.error_codes else "")
        )
        if i < _FULL_RESULT_THRESHOLD:
            if h.summary:
                header += f"\n   摘要: {h.summary}"
            for name, content in h.sections.items():
                header += f"\n   [{name}]: {content}"
        else:
            if h.summary:
                header += f" | 摘要:{h.summary}"
            elif h.sections:
                first = next(iter(h.sections.values()), "")
                if first:
                    header += f" | {first[:150]}{'…' if len(first) > 150 else ''}"
        parts.append(header)
    return "\n".join(parts)


def _build_chat_system(
    hits: list[DocHit] | None,
    search_status: SearchStatus | None,
    total: int = 0,
) -> str:
    base = (
        "你是半导体制造设备知识库助手。\n"
        "规则：只基于检索结果作答，不编造参数或步骤；"
        "不确定时说明；信息不足时追问项目/机台/报警代码/故障现象。用Markdown。"
    )

    if search_status == SearchStatus.TOO_MANY:
        return (
            base
            + f"\n\n检索匹配过多（约{total}条），引导用户缩小范围："
            "补充机台型号、报警代码或更具体的描述。"
        )

    if hits is None:
        return base + "\n\n尚未检索。请先了解用户需求再引导补充关键信息。"

    if not hits:
        return base + "\n\n检索无结果。帮助用户换描述或补充信息后重试。"

    note = ""
    if search_status == SearchStatus.LOOSE_HIT:
        note = "（宽松匹配，仅供参考）\n"
    elif search_status == SearchStatus.VECTOR_ONLY:
        note = "（语义匹配，置信度较低）\n"

    formatted = _format_results_for_llm(hits)
    return f"{base}\n\n{note}检索到{len(hits)}条文档：\n{formatted}"


def _sufficient_params(p: dict) -> bool:
    has_field = bool(
        p.get("project")
        or p.get("equipment")
        or p.get("error_codes")
        or p.get("knowledge_type")
    )
    has_kw = len(p.get("keywords") or []) >= 2
    return has_field or has_kw


async def _extract_from_conversation(
    settings, taxonomy, messages: list[_Message],
) -> dict:
    system = _build_extract_system(taxonomy)
    user_turns = [m.content for m in messages if m.role == "user"]
    if not user_turns:
        return {}

    if len(user_turns) == 1:
        query = user_turns[0]
    else:
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(user_turns))
        query = f"多轮对话：\n{numbered}\n\n基于全部上下文提取最新参数。"

    try:
        raw = await _call_llm(
            settings,
            [{"role": "system", "content": system}, {"role": "user", "content": query}],
            timeout=8.0,
        )
        return json.loads(_strip_code_fence(raw))
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("chat: param extraction failed — %s", exc)
        return {}


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    settings: SettingsDep,
    taxonomy_store: TaxonomyDep,
    search_service: SearchDep,
) -> ChatResponse:
    """Conversational KB search.

    Receives full conversation history, extracts search parameters,
    searches the knowledge base, and returns an LLM response informed
    by the search results. The LLM may answer or ask for clarification.
    """
    recent = body.messages[-_MAX_HISTORY:]

    # 1. Extract search params from conversation
    taxonomy = taxonomy_store.current
    extracted = await _extract_from_conversation(settings, taxonomy, recent)

    # 2. Search if params are sufficient
    search_resp = None
    if _sufficient_params(extracted):
        kt = None
        if kt_str := extracted.get("knowledge_type"):
            try:
                kt = KnowledgeType(kt_str)
            except ValueError:
                pass

        last_user = next(
            (m.content for m in reversed(recent) if m.role == "user"), ""
        )
        try:
            search_resp = await search_service.search(
                SearchRequest(
                    project=extracted.get("project"),
                    equipment=extracted.get("equipment"),
                    knowledge_type=kt,
                    error_codes=extracted.get("error_codes") or [],
                    keywords=extracted.get("keywords") or [],
                    query_text=last_user or None,
                    mode="auto",
                )
            )
        except Exception as exc:
            log.warning("chat: search failed — %s", exc)

    # 3. Build system prompt with search context
    hits = search_resp.hits if search_resp else None
    ss = search_resp.status if search_resp else None
    total = search_resp.total if search_resp else 0
    system = _build_chat_system(hits, ss, total)

    # 4. LLM call with full history
    msgs: list[dict] = [{"role": "system", "content": system}]
    msgs.extend(m.model_dump() for m in recent)
    content = await _call_llm(settings, msgs)

    return ChatResponse(
        content=content,
        search_results=search_resp.hits if search_resp and search_resp.hits else None,
        search_status=ss,
        effective_params=search_resp.effective_params if search_resp else None,
    )


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
    return f"""从用户查询中提取参数，只返回JSON。

枚举值（必须精确匹配，否则填null）：
- project: {projects}
- equipment: {equipment}
- knowledge_type: alarm, setup, experience

equipment规则：用户必须明确提到上述设备名才填写，仅描述部件或现象则填null。宁填null不猜。

{{"project":null,"knowledge_type":null,"error_codes":[],"equipment":null,"keywords":["关键词1","关键词2"],"is_sentence":false}}

字段说明：
- error_codes: 报警代码字符串列表，无则空数组
- keywords: 3-5个检索词，排除project和equipment
- is_sentence: 自然语言问句为true，关键词组合为false"""


@router.post("/extract", response_model=ExtractResponse)
async def extract_params(
    body: ExtractRequest,
    settings: SettingsDep,
    taxonomy_store: TaxonomyDep,
) -> ExtractResponse:
    """Use the LLM to extract structured search parameters from a free-text query."""
    taxonomy = taxonomy_store.current
    system = _build_extract_system(taxonomy)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": body.query},
    ]
    raw = await _call_llm(settings, messages, timeout=8.0)
    try:
        parsed = json.loads(_strip_code_fence(raw))
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
