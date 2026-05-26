# AI Chat Search Architecture

## Overview

The system is a **retrieval-only knowledge base** for semiconductor manufacturing equipment. The LLM is never a source of facts — it is used exclusively as a query parser and a conversational interface that explains verbatim document results. Two endpoints serve these roles:

- `POST /api/v1/chat` — full conversational search: parse → retrieve → respond
- `POST /api/v1/extract` — standalone NL-to-structured-params extraction only

---

## End-to-End Request Flow (`/chat`)

```
Client (full history + optional last_search_params)
        │
        ▼
[0] History management
        │  recent = last 20 messages
        │  if older messages exist → LLM summarizes them
        │
        ▼
[1] LLM: param extraction
        │  ← if last_search_params provided: update mode (modify existing params)
        │  ← else: fresh extraction from all user turns, numbered
        │  → {project, equipment, error_codes, keywords, knowledge_type}
        │
        ▼
[2] Sufficiency gate
        │  project/equipment/error_codes/knowledge_type OR ≥2 keywords?
        ├─ NO  → skip search, system prompt = "ask for more info"
        └─ YES ▼
               │
        [3] SearchService.search(mode="auto")
               │  strict → loose → vector_only pipeline (see Ranking section)
               │  → SearchResponse {status, hits, total, facets, banner}
               │
        ▼
[4] Build system prompt
        │  content depends on SearchStatus (see Context Construction)
        │  includes history summary if available
        │
        ▼
[5] LLM: conversational answer
        │  messages = [system_prompt] + recent_history (≤20 turns)
        │
        ▼
ChatResponse {content, search_results, search_status, effective_params}
```

---

## Query Understanding: Param Extraction

The LLM is called with a strict JSON-schema prompt (`_build_extract_system`) that lists the exact taxonomy values for `project` and `equipment`. The LLM must match these exactly or return `null` — it is instructed to prefer `null` over guessing.

**Two extraction modes**:

**Fresh extraction** (no `last_search_params`): extracts from all user turns in the conversation.

- *Single turn*: the raw user message is sent directly.
- *Multi-turn*: all user messages (not assistant messages) are numbered and concatenated:
```
多轮对话：
1. <first user turn>
2. <second user turn>
...
基于全部上下文提取最新参数。
```

**Update mode** (`last_search_params` provided): the client echoes the `effective_params` from the previous response. The LLM receives the current params plus the last 8 messages (both roles) and modifies the params incrementally — adding, removing, or changing fields as the user directs, while preserving fields the user did not mention.

If a history summary is available (see History Management), it is prepended to the extraction query in both modes.

The extraction call uses a short timeout (8 s) and fails silently to `{}` — the search gate then blocks the search and the LLM is asked to elicit more information instead.

**Extracted fields**:

| Field | Type | Notes |
|---|---|---|
| `project` | `str\|null` | Must match taxonomy exactly |
| `equipment` | `str\|null` | Must match taxonomy; only when explicitly named |
| `error_codes` | `list[str]` | Alarm code strings, e.g. `["E-1234"]` |
| `knowledge_type` | `alarm\|setup\|experience\|null` | Routes to the correct ES index |
| `keywords` | `list[str]` | 3–5 search terms, excluding project/equipment names |
| `is_sentence` | `bool` | True if the query is a natural language question |

---

## Search Pipeline: Ranking and Fallback

The pipeline is a **strict → loose → vector-only** state machine run by `SearchService._auto()`. Each stage produces a typed `SearchStatus`, and the machine short-circuits on success.

### Stage 1 — Strict (AND-keyword BM25 + vector rescore)

- ES `multi_match` across `title^{title_boost}` and `body`, operator `AND`
- Filter clauses (no score impact): `project`, `equipment`, `error_codes`
- **Gate**: if total hits > `strict_max_hits` (default 8) → return `TOO_MANY` with facet aggregations; do not return documents
- On `strict_hit`: optionally rescore top-`rrf_window` (default 50) candidates by blending BM25 + cosine vector similarity

### Stage 2 — Loose (OR-keyword BM25 + vector rescore)

- Same query structure but operator `OR` — any keyword match qualifies
- Same optional rescore step
- Returns `LOOSE_HIT`; the banner text "仅供参考" (for reference only) is a hard contract that callers must render

### Stage 3 — Vector-only (pure kNN)

- Only runs if `query_text` is present (the raw last user message)
- ES `knn` query on `body_vec`; `k = req.size`, `num_candidates = max(k*4, 100)`
- Filters (project/equipment/error_codes) still apply
- Returns `VECTOR_ONLY`; low-confidence banner is mandatory
- Requires the embedding service to be reachable — silently falls through to `NO_HIT` if it fails

### Ranking Formula

When the embedding service is available, stages 1 and 2 apply a rescore pass over the top `rrf_window` keyword-recall candidates:

```
final_score = (1 - vector_weight) × BM25_score
            + vector_weight × (cosine_similarity(query_vec, body_vec) + 1)
```

- `vector_weight` defaults to `0.5`; tunable via `KB_SEARCH__VECTOR_WEIGHT`
- `cosine_sim + 1` maps `[-1, 1]` → `[0, 2]` to keep scores non-negative
- Docs missing a `body_vec` (seeded without embeddings) score 0 on the vector component

When the embedding service is down, stages 1 and 2 run with BM25 only — no error, no degraded status flag.

### Status Contract

| `SearchStatus` | Condition | Documents returned |
|---|---|---|
| `strict_hit` | AND-keywords + filters matched, ≤ `strict_max_hits` | Yes |
| `too_many` | AND-keywords + filters matched, > `strict_max_hits` | No (facets only) |
| `loose_hit` | OR-keywords matched | Yes (with banner) |
| `vector_only` | Only kNN matched | Yes (with banner) |
| `no_hit` | All stages failed | No |

---

## Context Construction for the LLM

After retrieval, `_build_chat_system()` assembles the system prompt. The behavior varies by status:

| Condition | System prompt instruction |
|---|---|
| No search run (insufficient params) | Ask user to provide project / equipment / alarm code / symptom |
| `TOO_MANY` | Tell user ~N results matched; ask them to narrow by equipment, alarm code, or description |
| `NO_HIT` or empty hits | Tell user nothing matched; suggest rephrasing or adding details |
| `LOOSE_HIT` | Prefix results with "宽松匹配，仅供参考" |
| `VECTOR_ONLY` | Prefix results with "语义匹配，置信度较低" |
| `STRICT_HIT` | No qualifier |

**Document serialization** (`_format_results_for_llm`):

- Up to `_MAX_RESULTS_IN_CONTEXT = 2` documents are included
- Each hit shows: title, project, equipment, error codes (if any), and either the summary or the first 200 characters of the first section

The LLM system prompt enforces three rules in all cases:
1. Only answer from retrieved documents — never fabricate parameters or steps
2. Acknowledge uncertainty when present
3. Ask clarifying questions when information is insufficient (project / equipment / alarm code / symptom)

---

## User-Assistant Interaction Model

The conversational state is **stateless on the server** — the client sends the entire message history on every request. The server:

1. Caps the recent window at `_MAX_HISTORY = 20` messages
2. Summarizes messages older than the window via a separate LLM call (see History Management)
3. Extracts params — either fresh or incrementally via update mode
4. Re-runs the full search pipeline on each turn

This means the user can refine their query across multiple turns naturally — saying "actually it's the CMP machine" in turn 3 will update the extracted `equipment` and trigger a fresh search without any session state management.

### History Management

When the conversation exceeds 20 messages, older messages are summarized by a dedicated LLM call (`_summarize_older_history`). The summary extracts key information (project, equipment, alarm codes, symptoms, attempted solutions) in 2–3 sentences. This summary is:

- Prepended to the extraction query so params from early turns are not lost
- Included in the chat system prompt under an "早期对话摘要" (earlier conversation summary) section

If summarization fails (timeout or LLM error), the system proceeds without it — only the recent 20 messages are used.

### Incremental Param Update

The client can send `last_search_params` (the `effective_params` from the previous response) to enable update mode. Instead of re-extracting all params from scratch, the LLM sees the current params alongside recent conversation and applies only the changes the user expressed. This is more robust for long conversations where the user is incrementally refining a search.

**Clarification flow**: when the LLM determines it cannot give a useful answer (no results, too many, or parameters insufficient), the system prompt instructs it to ask for one of: project, equipment, alarm code, or fault description. The next user message is added to history, and the extraction step picks up the new information from the combined context.

**`effective_params` echo**: the response always includes what parameters were actually applied. This allows the frontend to display "searching MEM project, Sphere equipment for keywords: [...]" immediately, letting the user catch extraction errors before reading the LLM's answer. The client should echo this back as `last_search_params` on the next request to enable update mode.

---

## Configuration Knobs

All tunable via `config/settings.yaml` or `KB_SEARCH__*` env vars:

| Parameter | Default | Effect |
|---|---|---|
| `search.strict_max_hits` | `8` | TOO_MANY threshold |
| `search.title_boost` | `3.0` | Title field weight vs body in BM25 |
| `search.rrf_window` | `50` | How many recall hits are rescored by vector |
| `search.vector_weight` | `0.5` | Balance between BM25 and cosine in final score |
| `llm.max_tokens` | `1200` | Maximum tokens in LLM response |
| `embedding.batch_size` | `10` | Max docs per embedding API call |

---

## Key Design Constraints

- **No hallucination**: LLM responses are grounded exclusively in retrieved documents. The system prompt forbids generating parameters, steps, or explanations not present in the results.
- **Taxonomy enforcement**: `project` and `equipment` values are validated at index time; the LLM prompt lists valid values so extraction stays within the vocabulary.
- **Graceful embedding degradation**: vector ranking and kNN fallback are silently skipped when the embedding service is unavailable; BM25-only search continues normally.
- **Banners are a hard contract**: `LOOSE_HIT` and `VECTOR_ONLY` statuses carry mandatory display banners (`banner` field). Callers must render these verbatim — they signal reduced confidence to the user.
