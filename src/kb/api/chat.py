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

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from kb.api.deps import LLMDep, SearchDep, SettingsDep, TaxonomyDep
from kb.models.search import DocHit, EffectiveParams, SearchRequest, SearchStatus
from kb.models.taxonomy import KnowledgeType
from kb.observability import metrics
from kb.services.llm import LLMClient, LLMError, LLMNotConfiguredError

log = logging.getLogger("kb.chat")

router = APIRouter(prefix="/api/v1", tags=["chat"])

_MAX_HISTORY = 20
_MAX_RESULTS_IN_CONTEXT = 2


# ── Shared helpers ────────────────────────────────────────────────────────────

class _Message(BaseModel):
    role: str
    content: str


def _http_from_llm_error(exc: Exception) -> HTTPException:
    """Translate an LLM client failure into the right HTTP status."""
    if isinstance(exc, LLMNotConfiguredError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM not configured — set KB_LLM__API_KEY environment variable.",
        )
    log.warning("LLM call failed: %s", exc)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="LLM upstream error",
    )


def _canonical_taxonomy_value(value: object, valid: list[str]) -> str | None:
    """Map an LLM-supplied value to its canonical taxonomy casing, or None.

    The extraction LLM is told to emit exact taxonomy strings, but it can
    hallucinate or mis-case them. An unknown value would otherwise become a
    filter that silently matches nothing, so we drop it (and log) rather than
    search on a value the taxonomy doesn't contain.
    """
    if not value or not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    for candidate in valid:
        if candidate.lower() == lowered:
            return candidate
    log.info("Dropping LLM value %r — not in taxonomy", value)
    return None


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return text


# ── /chat — conversational search ───────────────────────────────────────────


class ChatRequest(BaseModel):
    messages: list[_Message]
    # Echo of effective_params from the previous response.  When present the
    # extraction LLM treats it as the current search state and modifies it;
    # when absent params are extracted fresh from the conversation.
    last_search_params: dict | None = None


class ChatResponse(BaseModel):
    content: str
    search_results: list[DocHit] | None = None
    search_status: SearchStatus | None = None
    effective_params: EffectiveParams | None = None
    # True when the KB search raised (e.g. Elasticsearch unreachable) rather
    # than legitimately returning no hits — lets the UI show a "retry" hint
    # instead of a "no knowledge found" message.
    search_error: bool = False


def _format_results_for_llm(hits: list[DocHit]) -> str:
    parts: list[str] = []
    for i, h in enumerate(hits[:_MAX_RESULTS_IN_CONTEXT]):
        header = (
            f"{i + 1}. 【{h.title}】项目:{h.project} 机台:{h.equipment}"
            + (f" 报警码:{','.join(h.error_codes)}" if h.error_codes else "")
        )
        if h.summary:
            header += f"\n   摘要: {h.summary}"
        elif h.sections:
            first = next(iter(h.sections.values()), "")
            if first:
                header += f"\n   {first[:200]}{'…' if len(first) > 200 else ''}"
        parts.append(header)
    return "\n".join(parts)


def _build_chat_system(
    hits: list[DocHit] | None,
    search_status: SearchStatus | None,
    total: int = 0,
    history_summary: str = "",
    search_failed: bool = False,
) -> str:
    base = (
        "你是半导体制造设备知识库助手。\n"
        "规则：只基于检索结果作答，不编造参数或步骤；"
        "不确定时说明；信息不足时追问项目/机台/报警代码/故障现象。用Markdown。"
    )
    if history_summary:
        base += f"\n\n【早期对话摘要】{history_summary}"

    if search_failed:
        # ES error, not a genuine no-hit — don't imply the KB is empty.
        return (
            base
            + "\n\n检索服务暂时不可用，无法查询知识库。请告知用户稍后重试，"
            "不要凭空作答或编造内容。"
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


async def _summarize_older_history(
    llm: LLMClient, settings, older: list[_Message]
) -> str:
    turns = "\n".join(f"[{m.role}]: {m.content}" for m in older)
    prompt = (
        f"以下是对话历史（较早部分）：\n{turns}\n\n"
        "请提取其中的关键信息（项目、机台、报警代码、故障现象、已尝试的方案），"
        "并用2-3句话概括本段对话的主题和结论。只输出摘要文本，不要JSON。"
    )
    try:
        raw = await llm.complete(
            [{"role": "user", "content": prompt}], timeout=settings.llm.extract_timeout_s
        )
        return raw.strip()
    except Exception as exc:
        log.warning("chat: history summarization failed — %s", exc)
        return ""


async def _extract_from_conversation(
    llm: LLMClient,
    settings,
    taxonomy,
    messages: list[_Message],
    history_summary: str = "",
    last_params: dict | None = None,
) -> dict:
    system = _build_extract_system(taxonomy, update_mode=last_params is not None)

    if last_params is not None:
        # Update mode: show current params + recent full conversation so the LLM
        # can apply changes the user expressed (add/remove/modify any field).
        conv_lines = "\n".join(
            f"[{m.role}]: {m.content}" for m in messages[-8:]
        )
        query = (
            f"当前检索参数：\n{json.dumps(last_params, ensure_ascii=False)}\n\n"
            f"最近对话：\n{conv_lines}\n\n"
            "根据对话更新检索参数并输出完整JSON。"
        )
    else:
        # Fresh extraction: use all user turns (original behaviour).
        user_turns = [m.content for m in messages if m.role == "user"]
        if not user_turns:
            return {}
        if len(user_turns) == 1:
            query = user_turns[0]
        else:
            numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(user_turns))
            query = f"多轮对话：\n{numbered}\n\n基于全部上下文提取最新参数。"

    if history_summary:
        query = f"【早期对话摘要】{history_summary}\n\n{query}"

    try:
        raw = await llm.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": query}],
            timeout=settings.llm.extract_timeout_s,
        )
        return json.loads(_strip_code_fence(raw))
    except LLMNotConfiguredError:
        # No key — let the final chat() call surface the 503 cleanly.
        return {}
    except Exception as exc:
        log.warning("chat: param extraction failed — %s", exc)
        return {}


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    settings: SettingsDep,
    taxonomy_store: TaxonomyDep,
    search_service: SearchDep,
    llm: LLMDep,
) -> ChatResponse:
    """Conversational KB search.

    Receives full conversation history, extracts search parameters,
    searches the knowledge base, and returns an LLM response informed
    by the search results. The LLM may answer or ask for clarification.
    """
    recent = body.messages[-_MAX_HISTORY:]

    # Summarize messages older than _MAX_HISTORY if present
    history_summary = ""
    if len(body.messages) > _MAX_HISTORY:
        older = body.messages[:-_MAX_HISTORY]
        history_summary = await _summarize_older_history(llm, settings, older)

    # 1. Extract search params from recent conversation (with historical context).
    # last_search_params, if provided, enables update mode so the LLM can
    # add/remove/modify individual fields rather than re-extracting from scratch.
    taxonomy = taxonomy_store.current
    extracted = await _extract_from_conversation(
        llm, settings, taxonomy, recent, history_summary,
        last_params=body.last_search_params,
    )

    # 2. Search if params are sufficient
    search_resp = None
    search_failed = False
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
        project = _canonical_taxonomy_value(extracted.get("project"), taxonomy.projects)
        equipment = _canonical_taxonomy_value(extracted.get("equipment"), taxonomy.equipment)
        try:
            search_resp = await search_service.search(
                SearchRequest(
                    project=project,
                    equipment=equipment,
                    knowledge_type=kt,
                    error_codes=extracted.get("error_codes") or [],
                    keywords=extracted.get("keywords") or [],
                    query_text=last_user or None,
                    mode="auto",
                )
            )
        except Exception as exc:
            # An exception here means the search backend failed (e.g. ES
            # unreachable) — distinct from a legitimate no-hit. Flag it so the
            # model is told retrieval is down rather than answering from nothing.
            log.warning("chat: search failed — %s", exc)
            metrics.record_upstream_error("es")
            search_failed = True

    # 3. Build system prompt with search context
    hits = search_resp.hits if search_resp else None
    ss = search_resp.status if search_resp else None
    total = search_resp.total if search_resp else 0
    system = _build_chat_system(hits, ss, total, history_summary, search_failed)

    # 4. LLM call with recent history
    msgs: list[dict] = [{"role": "system", "content": system}]
    msgs.extend(m.model_dump() for m in recent)
    try:
        content = await llm.complete(msgs, timeout=settings.llm.timeout_s)
    except (LLMNotConfiguredError, LLMError) as exc:
        raise _http_from_llm_error(exc) from exc

    return ChatResponse(
        content=content,
        search_results=search_resp.hits if search_resp and search_resp.hits else None,
        search_status=ss,
        effective_params=search_resp.effective_params if search_resp else None,
        search_error=search_failed,
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


def _build_extract_system(taxonomy, *, update_mode: bool = False) -> str:
    projects = ", ".join(taxonomy.projects)
    equipment = ", ".join(taxonomy.equipment)
    base = f"""枚举值（必须精确匹配，否则填null）：
- project: {projects}
- equipment: {equipment}
- knowledge_type: alarm, setup, experience

equipment规则：用户必须明确提到上述设备名才填写，仅描述部件或现象则填null。宁填null不猜。

字段说明：
- error_codes: 报警代码字符串列表，无则空数组
- keywords: 3-5个检索词，排除project和equipment
- is_sentence: 自然语言问句为true，关键词组合为false

只返回JSON，不要其他文字。
示例：{{"project":null,"knowledge_type":null,"error_codes":[],"equipment":null,"keywords":["关键词1","关键词2"],"is_sentence":false}}"""

    if update_mode:
        return (
            "你是检索参数更新助手。根据最新对话修改现有检索参数，输出完整JSON。\n\n"
            "更新规则：\n"
            "- 用户要求移除某字段 → 将该字段设为null（或空数组）\n"
            "- 用户要求添加条件 → 在现有值基础上追加\n"
            "- 用户完全改变话题 → 更新所有相关字段\n"
            "- 用户未提及的字段 → 保持不变\n\n"
            + base
        )

    return "从用户查询中提取检索参数，只返回JSON。\n\n" + base


@router.post("/extract", response_model=ExtractResponse)
async def extract_params(
    body: ExtractRequest,
    settings: SettingsDep,
    taxonomy_store: TaxonomyDep,
    llm: LLMDep,
) -> ExtractResponse:
    """Use the LLM to extract structured search parameters from a free-text query."""
    taxonomy = taxonomy_store.current
    system = _build_extract_system(taxonomy)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": body.query},
    ]
    try:
        raw = await llm.complete(messages, timeout=settings.llm.extract_timeout_s)
    except (LLMNotConfiguredError, LLMError) as exc:
        raise _http_from_llm_error(exc) from exc
    try:
        parsed = json.loads(_strip_code_fence(raw))
        return ExtractResponse(
            project=_canonical_taxonomy_value(parsed.get("project"), taxonomy.projects),
            knowledge_type=parsed.get("knowledge_type"),
            error_codes=parsed.get("error_codes") or [],
            equipment=_canonical_taxonomy_value(parsed.get("equipment"), taxonomy.equipment),
            keywords=parsed.get("keywords") or [],
            is_sentence=bool(parsed.get("is_sentence", False)),
        )
    except Exception as exc:
        log.warning("extract: failed to parse LLM JSON — %s | raw=%s", exc, raw[:200])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM returned unparseable response",
        )
