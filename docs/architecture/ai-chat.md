# AI Chat Search Architecture

The system is a **retrieval-only knowledge base** for semiconductor manufacturing
equipment. The LLM is never a source of facts — it is used exclusively as a query
parser and a conversational interface that explains verbatim document results. Two
endpoints serve these roles:

- `POST /api/v1/chat` — full conversational search: parse → retrieve → respond
- `POST /api/v1/extract` — standalone NL-to-structured-params extraction only

Implementation: `src/kb/api/chat.py`. Both endpoints return **HTTP 503** when
`KB_LLM__API_KEY` is unset.

---

## End-to-end request flow (`/chat`)

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
               │  strict → loose → vector_only pipeline (see Search & Ranking)
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
ChatResponse {content, search_results, search_status, effective_params, search_error}
```

---

## Query understanding: param extraction

The LLM is called with a strict JSON-schema prompt (`_build_extract_system`) that
lists the exact taxonomy values for `project` and `equipment`. The LLM must match
these exactly or return `null` — it is instructed to prefer `null` over guessing.
Returned values are then mapped to canonical taxonomy casing by
`_canonical_taxonomy_value`; anything not in the taxonomy is dropped (and logged)
so it can't become a filter that silently matches nothing.

**Two extraction modes:**

**Fresh extraction** (no `last_search_params`): extracts from all user turns in the
conversation.

- *Single turn*: the raw user message is sent directly.
- *Multi-turn*: all user messages (not assistant messages) are numbered and
  concatenated:

```
多轮对话：
1. <first user turn>
2. <second user turn>
...
基于全部上下文提取最新参数。
```

**Update mode** (`last_search_params` provided): the client echoes the
`effective_params` from the previous response. The LLM receives the current params
plus the last 8 messages (both roles) and modifies the params incrementally —
adding, removing, or changing fields as the user directs, while preserving fields
the user did not mention.

If a history summary is available (see [History management](#history-management)),
it is prepended to the extraction query in both modes.

The extraction call uses a short timeout (`llm.extract_timeout_s`, default 10 s)
and fails silently to `{}` — the search gate then blocks the search and the LLM is
asked to elicit more information instead.

**Extracted fields:**

| Field | Type | Notes |
|---|---|---|
| `project` | `str\|null` | Must match taxonomy exactly |
| `equipment` | `str\|null` | Must match taxonomy; only when explicitly named |
| `error_codes` | `list[str]` | Alarm code strings, e.g. `["E-1234"]` |
| `knowledge_type` | `alarm\|setup\|experience\|null` | Routes to the correct ES index |
| `keywords` | `list[str]` | 3–5 search terms, excluding project/equipment names |
| `is_sentence` | `bool` | True if the query is a natural language question |

---

## Search pipeline: ranking and fallback

The pipeline is a **strict → loose → vector-only** state machine run by
`SearchService._auto()`. Each stage produces a typed `SearchStatus`, and the
machine short-circuits on success. The full ranking formula and status contract are
documented in [Search & Ranking](search-ranking.md); the summary as seen by `/chat`:

| `SearchStatus` | Condition | Documents returned |
|---|---|---|
| `strict_hit` | AND-keywords + filters matched, ≤ `strict_max_hits` | Yes |
| `too_many` | AND-keywords + filters matched, > `strict_max_hits` | No (facets only) |
| `loose_hit` | OR-keywords matched | Yes (with banner) |
| `vector_only` | Only kNN matched | Yes (with banner) |
| `no_hit` | All stages failed | No |

---

## Context construction for the LLM

After retrieval, `_build_chat_system()` assembles the system prompt. The behavior
varies by status:

| Condition | System prompt instruction |
|---|---|
| No search run (insufficient params) | Ask user to provide project / equipment / alarm code / symptom |
| Search backend error (`search_error`) | Tell user retrieval is temporarily down; **do not** imply the KB is empty or answer from nothing |
| `too_many` | Tell user ~N results matched; ask them to narrow by equipment, alarm code, or description |
| `no_hit` or empty hits | Tell user nothing matched; suggest rephrasing or adding details |
| `loose_hit` | Prefix results with "宽松匹配，仅供参考" (loose match, for reference only) |
| `vector_only` | Prefix results with "语义匹配，置信度较低" (semantic match, low confidence) |
| `strict_hit` | No qualifier |

**Document serialization** (`_format_results_for_llm`):

- Up to `_MAX_RESULTS_IN_CONTEXT = 2` documents are included.
- Each hit shows: title, project, equipment, error codes (if any), and either the
  summary or the first 200 characters of the first section.

The LLM system prompt enforces three rules in all cases:

1. Only answer from retrieved documents — never fabricate parameters or steps.
2. Acknowledge uncertainty when present.
3. Ask clarifying questions when information is insufficient (project / equipment /
   alarm code / symptom).

!!! info "Search error ≠ no hit"
    If the search backend raises (e.g. Elasticsearch unreachable), the handler
    sets `search_error=True` rather than treating it as a legitimate `no_hit`. The
    system prompt then tells the model retrieval is down, and the response carries
    `search_error: true` so the UI can show a *retry* hint instead of a "no
    knowledge found" message.

---

## User–assistant interaction model

The conversational state is **stateless on the server** — the client sends the
entire message history on every request. The server:

1. Caps the recent window at `_MAX_HISTORY = 20` messages.
2. Summarizes messages older than the window via a separate LLM call.
3. Extracts params — either fresh or incrementally via update mode.
4. Re-runs the full search pipeline on each turn.

So the user can refine their query across turns naturally — saying "actually it's
the CMP machine" in turn 3 updates the extracted `equipment` and triggers a fresh
search without any server-side session state.

Request payloads are bounded defensively: per-message `_MAX_MESSAGE_CHARS = 20_000`
and per-conversation `_MAX_MESSAGES = 200`, so a caller can't drive unbounded memory
or LLM token cost.

### History management

When the conversation exceeds 20 messages, older messages are summarized by a
dedicated LLM call (`_summarize_older_history`). The summary extracts key
information (project, equipment, alarm codes, symptoms, attempted solutions) in 2–3
sentences. It is:

- Prepended to the extraction query so params from early turns are not lost.
- Included in the chat system prompt under an "早期对话摘要" (earlier conversation
  summary) section.

If summarization fails (timeout or LLM error), the system proceeds without it — only
the recent 20 messages are used.

### Incremental param update

The client can send `last_search_params` (the `effective_params` from the previous
response) to enable update mode. Instead of re-extracting all params from scratch,
the LLM sees the current params alongside recent conversation and applies only the
changes the user expressed. This is more robust for long conversations where the
user is incrementally refining a search.

**`effective_params` echo:** the response always includes what parameters were
actually applied. This lets the frontend display "searching MEM project, Aligner
equipment for keywords: [...]" immediately, so the user can catch extraction errors
before reading the LLM's answer. The client should echo this back as
`last_search_params` on the next request to enable update mode.

---

## Configuration knobs

All tunable via `config/settings.yaml` or `KB_*` env vars. See
[Configuration](../configuration.md) for the full set.

| Parameter | Default | Effect |
|---|---|---|
| `search.strict_max_hits` | `8` | `too_many` threshold |
| `search.title_boost` | `3.0` | Title field weight vs body in BM25 |
| `search.rrf_window` | `50` | How many recall hits are rescored by vector |
| `search.vector_weight` | `0.5` | Balance between BM25 and cosine in final score |
| `llm.max_tokens` | `1200` | Maximum tokens in LLM response |
| `llm.timeout_s` | `20` | Read-timeout for the conversational answer call |
| `llm.extract_timeout_s` | `10` | Read-timeout for the `/extract` param call |

---

## Key design constraints

- **No hallucination** — LLM responses are grounded exclusively in retrieved
  documents. The system prompt forbids generating parameters, steps, or
  explanations not present in the results.
- **Taxonomy enforcement** — `project`/`equipment` are validated at index time; the
  LLM prompt lists valid values so extraction stays within the vocabulary.
- **Graceful embedding degradation** — vector ranking and kNN fallback are silently
  skipped when the embedding service is unavailable; BM25-only search continues.
- **Banners are a hard contract** — `loose_hit` and `vector_only` carry mandatory
  display banners (`banner` field). Callers must render these verbatim — they
  signal reduced confidence to the user.
