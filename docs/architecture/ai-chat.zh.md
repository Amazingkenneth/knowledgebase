# AI 对话搜索架构

本系统是一个面向半导体制造设备的**纯检索型知识库**。LLM 从不作为事实来源——它仅用于解析查询意图，以及作为对话界面向用户解释原文档内容。两个接口承担各自职责：

- `POST /api/v1/chat` — 完整对话搜索：解析 → 检索 → 回答
- `POST /api/v1/extract` — 仅执行自然语言到结构化参数的独立提取

实现位于 `src/kb/api/chat.py`。未配置 `KB_LLM__API_KEY` 时两个接口均返回 **HTTP 503**。

---

## 端到端请求流程（`/chat`）

```
客户端（完整对话历史 + 可选 last_search_params）
        │
        ▼
[0] 历史管理
        │  recent = 最近 20 条消息
        │  若存在更早消息 → LLM 摘要
        │
        ▼
[1] LLM：参数提取
        │  ← 若提供 last_search_params：增量更新模式（修改现有参数）
        │  ← 否则：从所有用户轮次全新提取，按编号拼接
        │  → {project, equipment, error_codes, keywords, knowledge_type}
        │
        ▼
[2] 参数充分性校验
        │  包含 project/equipment/error_codes/knowledge_type 之一，或关键词 ≥2 个？
        ├─ 否  → 跳过搜索，系统提示 = "引导用户补充信息"
        └─ 是  ▼
               │
        [3] SearchService.search(mode="auto")
               │  严格 → 宽松 → 纯向量 三级检索管道（见检索与排序）
               │  → SearchResponse {status, hits, total, facets, banner}
               │
        ▼
[4] 构建系统提示
        │  内容因 SearchStatus 而异（见上下文构建章节）
        │  包含历史摘要（若存在）
        │
        ▼
[5] LLM：生成对话回答
        │  messages = [system_prompt] + 最近历史（≤20 轮）
        │
        ▼
ChatResponse {content, search_results, search_status, effective_params, search_error}
```

---

## 查询理解：参数提取

LLM 接收一个严格的 JSON Schema 提示（`_build_extract_system`），其中列出了 `project` 和 `equipment` 的全部合法枚举值。LLM 必须精确匹配，否则返回 `null`——明确要求宁填 `null` 也不猜测。返回值随后由 `_canonical_taxonomy_value` 映射为规范大小写；不在 taxonomy 中的值会被丢弃（并记日志），以免成为静默匹配不到任何内容的过滤条件。

**两种提取模式**：

**全新提取**（无 `last_search_params`）：从对话中所有用户轮次提取参数。

- *单轮对话*：直接发送原始用户消息。
- *多轮对话*：将所有用户消息（不含助手消息）编号后拼接：

```
多轮对话：
1. <第一轮用户消息>
2. <第二轮用户消息>
...
基于全部上下文提取最新参数。
```

**增量更新模式**（提供 `last_search_params`）：客户端回传上次响应的 `effective_params`。LLM 接收当前参数加最近 8 条消息（含双方），增量修改参数——根据用户指示添加、移除或更改字段，未提及的字段保持不变。

若存在历史摘要（见[历史管理](#history-management)），两种模式均会将其前置于提取查询中。

提取调用使用较短超时（`llm.extract_timeout_s`，默认 10 秒），失败时静默返回 `{}`——充分性校验随即阻断搜索，LLM 改为向用户追问。

**提取字段说明**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `project` | `str\|null` | 必须精确匹配 taxonomy 枚举值 |
| `equipment` | `str\|null` | 必须精确匹配；仅在用户明确提及时填写 |
| `error_codes` | `list[str]` | 报警代码字符串列表，如 `["E-1234"]` |
| `knowledge_type` | `alarm\|setup\|experience\|null` | 决定路由到哪个 ES 索引 |
| `keywords` | `list[str]` | 3–5 个检索词，不包含 project/equipment 名称 |
| `is_sentence` | `bool` | 自然语言问句为 true，关键词组合为 false |

---

## 搜索管道：排序与降级策略

管道为**严格 → 宽松 → 纯向量**三级状态机，由 `SearchService._auto()` 驱动。每一级产生带类型的 `SearchStatus`，命中即短路返回。完整的排序公式与状态契约见[检索与排序](search-ranking.md)；从 `/chat` 视角的概要：

| `SearchStatus` | 触发条件 | 是否返回文档 |
|---|---|---|
| `strict_hit` | AND 关键词 + 过滤条件命中，且 ≤ `strict_max_hits` | 是 |
| `too_many` | AND 关键词 + 过滤条件命中，但 > `strict_max_hits` | 否（仅返回分面） |
| `loose_hit` | OR 关键词命中 | 是（附 banner） |
| `vector_only` | 仅 kNN 命中 | 是（附 banner） |
| `no_hit` | 三级均未命中 | 否 |

---

## LLM 上下文构建

检索完成后，`_build_chat_system()` 拼装系统提示，内容随状态变化：

| 条件 | 系统提示指令 |
|---|---|
| 参数不足，未触发搜索 | 引导用户提供项目 / 机台 / 报警码 / 故障现象 |
| 检索后端报错（`search_error`） | 告知用户检索暂时不可用；**不得**暗示知识库为空，也不得凭空作答 |
| `too_many` | 告知用户约有 N 条匹配，请缩小范围（机台、报警码或更具体描述） |
| `no_hit` 或结果为空 | 告知用户未找到匹配，建议换描述或补充信息 |
| `loose_hit` | 在结果前注明"宽松匹配，仅供参考" |
| `vector_only` | 在结果前注明"语义匹配，置信度较低" |
| `strict_hit` | 无附加说明 |

**文档序列化**（`_format_results_for_llm`）：

- 最多将 `_MAX_RESULTS_IN_CONTEXT = 2` 条文档注入上下文。
- 每条展示：标题、项目、机台、报警码（如有），以及 summary 或首个 section 的前 200 个字符。

系统提示在所有状态下均强制执行三条规则：

1. 只基于检索结果作答——不编造参数或步骤。
2. 不确定时明确说明。
3. 信息不足时追问（项目 / 机台 / 报警码 / 故障现象）。

!!! info "检索错误 ≠ 无结果"
    若检索后端抛出异常（如 Elasticsearch 不可达），处理函数会置 `search_error=True`，而非当作正常的 `no_hit`。此时系统提示会告知模型检索已宕机，且响应携带 `search_error: true`，使 UI 可展示*重试*提示而非"未找到知识"。

---

## 用户-助手交互模型

**服务端无状态**——客户端每次请求都发送完整对话历史。服务端：

1. 截取最近 `_MAX_HISTORY = 20` 条消息。
2. 对超出窗口的更早消息通过单独 LLM 调用生成摘要。
3. 提取参数——全新提取或通过增量更新模式。
4. 每轮都完整执行搜索管道。

因此用户可以在多轮对话中自然地细化查询——第 3 轮说"其实是 CMP 机台"，`equipment` 的提取结果会随即更新并触发新一轮搜索，无需任何服务端会话管理。

请求体有防御性上限：单条消息 `_MAX_MESSAGE_CHARS = 20_000`，单次对话 `_MAX_MESSAGES = 200`，使调用方无法驱动无界的内存或 LLM token 开销。

### 历史管理 {#history-management}

当对话超过 20 条消息时，更早的消息由专用 LLM 调用（`_summarize_older_history`）生成摘要。摘要提取关键信息（项目、机台、报警码、故障现象、已尝试方案），浓缩为 2-3 句话。该摘要会：

- 前置于参数提取查询中，避免丢失早期轮次的参数。
- 以"早期对话摘要"的形式包含在对话系统提示中。

摘要生成失败（超时或 LLM 错误）时，系统照常运行——仅使用最近 20 条消息。

### 增量参数更新

客户端可发送 `last_search_params`（上次响应的 `effective_params`）以启用增量更新模式。LLM 不再从头提取全部参数，而是在现有参数基础上结合最近对话仅应用用户表达的变更。这在长对话中用户逐步细化搜索时更为稳健。

**`effective_params` 回显**：响应始终包含实际生效的搜索参数。前端可据此立即展示"正在搜索 MEM 项目、Aligner 机台，关键词：[...]"，让用户在阅读 LLM 回答前及时发现提取错误。客户端应在下次请求中将其作为 `last_search_params` 回传以启用增量更新模式。

---

## 可调配置项

均可通过 `config/settings.yaml` 或 `KB_*` 环境变量设置，完整清单见[配置](../configuration.md)。

| 参数 | 默认值 | 作用 |
|---|---|---|
| `search.strict_max_hits` | `8` | `too_many` 阈值 |
| `search.title_boost` | `3.0` | BM25 中标题字段相对正文的权重 |
| `search.rrf_window` | `50` | 参与向量重排的召回候选数 |
| `search.vector_weight` | `0.5` | 最终评分中向量分量的权重 |
| `llm.max_tokens` | `1200` | LLM 单次回复最大 token 数 |
| `llm.timeout_s` | `20` | 对话回答调用的读超时 |
| `llm.extract_timeout_s` | `10` | `/extract` 参数调用的读超时 |

---

## 关键设计约束

- **禁止幻觉**：LLM 回答必须完全基于检索文档，系统提示明确禁止生成结果中未出现的参数、步骤或说明。
- **Taxonomy 约束**：`project` 和 `equipment` 在入库时校验；LLM 提示中列出合法枚举值，确保提取结果在已知词汇表范围内。
- **Embedding 优雅降级**：当 embedding 服务不可用时，向量重排和 kNN 降级步骤均静默跳过，BM25 搜索正常继续。
- **Banner 为强制契约**：`loose_hit` 和 `vector_only` 状态携带必须展示的 banner（`banner` 字段），调用方必须原文渲染，以向用户明示置信度降低。
