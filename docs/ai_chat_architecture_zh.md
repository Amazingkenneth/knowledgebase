# AI 对话搜索架构说明

## 概述

本系统是一个面向半导体制造设备的**纯检索型知识库**。LLM 从不作为事实来源——它仅用于解析查询意图，以及作为对话界面向用户解释原文档内容。两个接口承担各自职责：

- `POST /api/v1/chat` — 完整对话搜索：解析 → 检索 → 回答
- `POST /api/v1/extract` — 仅执行自然语言到结构化参数的独立提取

---

## 端到端请求流程（`/chat`）

```
客户端（完整对话历史）
        │
        ▼
[1] LLM：参数提取
        │  ← 对话中所有用户轮次，按编号拼接
        │  → {project, equipment, error_codes, keywords, knowledge_type}
        │
        ▼
[2] 参数充分性校验
        │  包含 project/equipment/error_codes/knowledge_type 之一，或关键词 ≥2 个？
        ├─ 否  → 跳过搜索，系统提示 = "引导用户补充信息"
        └─ 是  ▼
               │
        [3] SearchService.search(mode="auto")
               │  严格 → 宽松 → 纯向量 三级检索管道（见排序章节）
               │  → SearchResponse {status, hits, total, facets, banner}
               │
        ▼
[4] 构建系统提示
        │  内容因 SearchStatus 而异（见上下文构建章节）
        │
        ▼
[5] LLM：生成对话回答
        │  messages = [system_prompt] + 最近历史（≤20 轮）
        │
        ▼
ChatResponse {content, search_results, search_status, effective_params}
```

---

## 查询理解：参数提取

LLM 接收一个严格的 JSON Schema 提示（`_build_extract_system`），其中列出了 `project` 和 `equipment` 的全部合法枚举值。LLM 必须精确匹配，否则返回 `null`——明确要求宁填 `null` 也不猜测。

**单轮对话**：直接发送原始用户消息。

**多轮对话**：将所有用户消息（不含助手消息）编号后拼接：
```
多轮对话：
1. <第一轮用户消息>
2. <第二轮用户消息>
...
基于全部上下文提取最新参数。
```

后续轮次可以细化或覆盖前轮信息。提取调用超时为 8 秒，失败时静默返回 `{}`——充分性校验随即阻断搜索，LLM 改为向用户追问。

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

管道为**严格 → 宽松 → 纯向量**三级状态机，由 `SearchService._auto()` 驱动。每一级产生带类型的 `SearchStatus`，命中即短路返回。

### 第一级 — 严格检索（AND 关键词 BM25 + 向量重排）

- ES `multi_match`，字段为 `title^{title_boost}` 和 `body`，operator 为 `AND`
- 过滤子句（不影响评分）：`project`、`equipment`、`error_codes`
- **门控**：总命中数 > `strict_max_hits`（默认 8）→ 返回 `TOO_MANY` 及分面聚合，不返回文档
- 命中时：对 top-`rrf_window`（默认 50）候选文档做 BM25 + 余弦向量相似度融合重排

### 第二级 — 宽松检索（OR 关键词 BM25 + 向量重排）

- 结构与严格检索相同，但 operator 为 `OR`——任意关键词匹配即可
- 同样执行可选的重排步骤
- 返回 `LOOSE_HIT`；banner 文字"仅供参考"是强制契约，调用方必须展示

### 第三级 — 纯向量检索（kNN）

- 仅在 `query_text` 存在时执行（即最后一条原始用户消息）
- ES `knn` 查询，字段为 `body_vec`；`k = req.size`，`num_candidates = max(k×4, 100)`
- project/equipment/error_codes 过滤条件仍然生效
- 返回 `VECTOR_ONLY`；低置信度 banner 为必填
- 依赖 embedding 服务可达——不可达时静默降级至 `NO_HIT`

### 评分公式

当 embedding 服务可用时，第一、二级对 top-`rrf_window` 关键词召回候选执行重排：

```
final_score = (1 - vector_weight) × BM25_score
            + vector_weight × (cosine_similarity(query_vec, body_vec) + 1)
```

- `vector_weight` 默认为 `0.5`，可通过 `KB_SEARCH__VECTOR_WEIGHT` 调整
- `cosine_sim + 1` 将 `[-1, 1]` 映射到 `[0, 2]`，确保分数非负
- 缺少 `body_vec` 的文档（未生成 embedding 时入库）在向量分量上得 0 分

当 embedding 服务不可用时，第一、二级仅使用 BM25——无报错，状态也不降级。

### 状态契约

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
| `TOO_MANY` | 告知用户约有 N 条匹配，请缩小范围（机台、报警码或更具体描述） |
| `NO_HIT` 或结果为空 | 告知用户未找到匹配，建议换描述或补充信息 |
| `LOOSE_HIT` | 在结果前注明"宽松匹配，仅供参考" |
| `VECTOR_ONLY` | 在结果前注明"语义匹配，置信度较低" |
| `STRICT_HIT` | 无附加说明 |

**文档序列化**（`_format_results_for_llm`）：

- 最多将 `_MAX_RESULTS_IN_CONTEXT = 6` 条文档注入上下文
- **前 3 条**（`_FULL_RESULT_THRESHOLD`）：完整 sections 字典 + summary
- **第 4–6 条**：仅 summary，或首个 section 的前 150 个字符——降低低排名文档的 token 消耗

系统提示在所有状态下均强制执行三条规则：
1. 只基于检索结果作答——不编造参数或步骤
2. 不确定时明确说明
3. 信息不足时追问（项目 / 机台 / 报警码 / 故障现象）

---

## 用户-助手交互模型

**服务端无状态**——客户端每次请求都发送完整对话历史。服务端：

1. 截取最近 `_MAX_HISTORY = 20` 条消息
2. 每轮都从完整历史重新执行参数提取
3. 每轮都完整执行搜索管道

因此，用户可以在多轮对话中自然地细化查询——第 3 轮说"其实是 CMP 机台"，`equipment` 的提取结果会随即更新，并触发新一轮搜索，无需任何服务端会话管理。

**澄清追问流程**：当 LLM 判断无法给出有效答案（无结果、结果过多或参数不足）时，系统提示会指示其追问以下信息之一：项目、机台、报警码或故障现象。用户的下一条消息追加到历史后，提取步骤会从合并上下文中获取新信息。

**`effective_params` 回显**：响应始终包含实际生效的搜索参数。前端可据此立即展示"正在搜索 MEM 项目、Sphere 机台，关键词：[...]"，让用户在阅读 LLM 回答前及时发现提取错误。

---

## 可调配置项

均可通过 `config/settings.yaml` 或 `KB_SEARCH__*` 环境变量设置：

| 参数 | 默认值 | 作用 |
|---|---|---|
| `search.strict_max_hits` | `8` | TOO_MANY 阈值 |
| `search.title_boost` | `3.0` | BM25 中标题字段相对正文的权重 |
| `search.rrf_window` | `50` | 参与向量重排的召回候选数 |
| `search.vector_weight` | `0.5` | 最终评分中向量分量的权重 |
| `llm.max_tokens` | `1200` | LLM 单次回复最大 token 数 |
| `embedding.batch_size` | `10` | 每次 embedding API 调用的最大文档数 |

---

## 关键设计约束

- **禁止幻觉**：LLM 回答必须完全基于检索文档，系统提示明确禁止生成结果中未出现的参数、步骤或说明。
- **Taxonomy 约束**：`project` 和 `equipment` 在入库时校验；LLM 提示中列出合法枚举值，确保提取结果在已知词汇表范围内。
- **Embedding 优雅降级**：当 embedding 服务不可用时，向量重排和 kNN 降级步骤均静默跳过，BM25 搜索正常继续。
- **Banner 为强制契约**：`LOOSE_HIT` 和 `VECTOR_ONLY` 状态携带必须展示的 banner（`banner` 字段），调用方必须原文渲染，以向用户明示置信度降低。
