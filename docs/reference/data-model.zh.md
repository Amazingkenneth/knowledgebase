# 数据模型与索引映射参考 {#data-model}

本页是**可复现级别**的数据契约参考：把每一个文档字段、Pydantic 校验规则、
Elasticsearch 映射、`body` 文本拼装格式、文档 ID 生成算法、辅助索引、API 请求/响应
模型，以及 CSV / 知识类型规格的字段对应关系全部列出。只要照此实现，即可得到与本项目
逐字节兼容的索引结构。

源代码位置：

| 关注点 | 文件 |
|---|---|
| 文档模型（`AlarmDoc` / `SetupDoc` / `ExperienceDoc`） | `src/kb/models/document.py` |
| 检索请求/响应模型 | `src/kb/models/search.py` |
| 导入管道模型 | `src/kb/models/ingest.py` |
| 分类法模型 | `src/kb/models/taxonomy.py` |
| 主索引映射 | `src/kb/es/mappings.py` |
| `body` 文本拼装 | `src/kb/es/body_builder.py` |
| 文档 ID + 索引时校验 | `src/kb/services/indexing.py` |
| 导入跟踪索引映射 | `src/kb/es/import_mappings.py` |
| 检索反馈索引映射 | `src/kb/es/feedback_mappings.py` |
| 知识类型规格（分段提示词） | `src/kb/services/spec.py`、`config/knowledge_types/*.yaml` |
| CSV → 文档映射 | `src/kb/services/csv_loader.py` |

---

## 知识类型枚举 {#knowledge-type}

`KnowledgeType`（`StrEnum`）是整个系统的根枚举，决定文档进入哪个索引、用哪个子类校验、
走哪套分段提示词：

| 取值 | 中文含义 | 文档子类 | 索引别名 |
|---|---|---|---|
| `alarm` | 机台报警 | `AlarmDoc` | `kb_alarm` |
| `setup` | 机台 setup / 调试规范 | `SetupDoc` | `kb_setup` |
| `experience` | 设备经验 / 故障案例 | `ExperienceDoc` | `kb_experience` |

新增知识类型需要同时改动：`KnowledgeType` 枚举、新增一个 `DocumentBase` 子类、
`doc_class_for()` 的 `match`、`config/knowledge_types/<type>.yaml` 规格文件、以及
`config/taxonomy.yaml` 的 `knowledge_types` 列表。

---

## 标量类型别名 {#scalar-aliases}

`src/kb/models/document.py` 顶部定义了几个带约束的字符串别名，所有文档字段都复用它们：

| 别名 | 定义 | 约束 |
|---|---|---|
| `NonEmptyStr` | `Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]` | 去除首尾空白后至少 1 个字符 |
| `TitleStr` | `Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]` | 标题：1–200 字符 |
| `SummaryStr` | `Annotated[str, StringConstraints(max_length=50, strip_whitespace=True)]` | 摘要：≤50 字符 |

报警码校验正则（`error_codes` 字段使用）：

```python
ERROR_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_\-]{0,63}$")
```

即：首字符为大写字母或数字，其后可接大写字母 / 数字 / 下划线 / 连字符，总长 1–64。
校验器会先 `strip().upper()`，再做唯一性检查，任何不匹配项都会抛出 `ValueError`。

---

## 公共文档字段 `DocumentBase` {#document-base}

所有知识文档的共同基类。字段分三组（与 ES 映射一一对应，见 [索引映射](#es-mappings)）：

| 字段 | 类型 | 默认 | 组别 | 说明 |
|---|---|---|---|---|
| `knowledge_type` | `KnowledgeType` | —（由子类锁定） | Part 1 | 文档类型 |
| `project` | `str` | `""` | Part 1 | 项目，索引时校验是否在分类法内 |
| `equipment` | `str` | `""` | Part 1 | 设备/机台，索引时校验是否在分类法内 |
| `error_codes` | `list[str]` | `[]` | Part 1 | 报警码列表，逐项匹配 `ERROR_CODE_RE`，转大写、去重 |
| `title` | `TitleStr` | 必填 | Part 3 | 标题，参与 BM25 与向量召回 |
| `source_file` | `str \| None` | `None` | Part 2 | 来源文件名（仅展示，不索引） |
| `source_pages` | `list[str]` | `[]` | Part 2 | 来源页码（仅展示，不索引） |
| `summary` | `SummaryStr \| None` | `None` | Part 2 | ≤50 字摘要，用于结果列表与控制 LLM 上下文长度 |
| `created_at` | `datetime \| None` | `None` | 审计 | 创建时间 |
| `updated_at` | `datetime \| None` | `None` | 审计 | 更新时间 |

!!! note "三组字段的检索语义"
    - **Part 1（keyword）**：精确过滤（`term`/`terms`），不分词，不影响 BM25 打分。
    - **Part 2（display-only）**：`index: False` / `enabled: False`，逐字返回用于渲染，
      永不参与查询或打分。
    - **Part 3（full-text）**：用配置的分析器分词，参与 BM25 关键词召回与向量重排。

每个子类必须实现 `content_sections() -> list[tuple[str, str]]`，返回有序的
`(段名, 文本)` 对。`body_builder` 用它拼装 `body` 与 `sections`（段名本身**不会**进入
`body` 文本，仅供 builder 跳过空段）。

---

## 三个文档子类 {#document-subclasses}

### `AlarmDoc`（报警）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `content` | `NonEmptyStr` | 必填 | 报警内容 / 触发条件，逐字 |
| `resolution` | `NonEmptyStr` | 必填 | 解除流程，逐字 |
| `notes` | `str` | `""` | 注意事项（可选） |

`content_sections()` 顺序：`content` → `resolution` →（`notes` 非空时）`notes`。

### `SetupDoc`（调试）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `procedure` | `NonEmptyStr` | 必填 | 调试步骤，逐字 |
| `prerequisites` | `str` | `""` | 规格/要求、工具等前置条件 |
| `notes` | `str` | `""` | 注意事项（可选） |

`content_sections()` 顺序：（`prerequisites` 非空时）`prerequisites` → `procedure` →
（`notes` 非空时）`notes`。

### `ExperienceDoc`（经验）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `body_text` | `NonEmptyStr` | 必填 | 失败描述（含失败分析/根因的拼接正文），逐字 |
| `procedure` | `str` | `""` | 纠正步骤 |
| `notes` | `str` | `""` | 注意事项（可选） |

`content_sections()` 顺序：`body` → （`procedure` 非空时）`procedure` →
（`notes` 非空时）`notes`。

!!! tip "判别联合（discriminated union）"
    `KnowledgeDoc = AlarmDoc | SetupDoc | ExperienceDoc`。每个子类的
    `knowledge_type` 字段被注解为 `Literal[...]` 锁死为自身类型，因此
    `_parse_doc()` 注入 `knowledge_type` 后只有匹配子类能通过校验。
    `doc_class_for(kt)` 用 `match` 把枚举映射回子类。

---

## `body` 文本拼装格式 {#body-builder}

`build_body(doc)`（`src/kb/es/body_builder.py`）按固定格式拼出索引到 ES `body` 字段的
文本。**该格式被测试钉死，修改必须升级索引版本并 reindex。**

分隔符常量：

```python
CONTENT_SEPARATOR = "\n\n---\n\n"   # 段与段、段与元数据块之间
META_SEPARATOR    = "\n"             # 元数据块内部行间
```

布局（以一篇含 notes 的报警为例）：

```
<content 文本>

---

<resolution 文本>

---

<notes 文本>

---

<title>
project: <project>
equipment: <equipment>
error_codes: <code1> <code2> ...
```

要点：

- 先按 `content_sections()` 顺序拼接各段（用 `CONTENT_SEPARATOR` 连接）。
- 最后追加一个**元数据块**：第一行是 `title`，随后是 `project: …`、`equipment: …`，
  若有报警码再加 `error_codes: c1 c2`（空格分隔）。整个块用 `META_SEPARATOR` 连接。
- 之所以把元数据并入 `body`，是为了让关键词查询（如搜 "MHK"）即使该词只出现在
  `project` 字段也能召回——分词器会把 `project:` 与 `MHK` 切开。

`build_title_text(doc)` 目前直接返回 `doc.title`（保留此函数只为与 `build_body` 对称，
便于将来在索引标题前拼 project/equipment）。

`title` 与 `body` 各自生成一个向量：`title_vec`、`body_vec`（见 [索引映射](#es-mappings)）。
当前检索仅在 `body_vec` 上做向量重排与 kNN。

---

## 文档 ID 生成 {#doc-id}

`doc_id(doc)`（`src/kb/services/indexing.py`）生成**内容寻址**的稳定 ID，保证同一逻辑
文档幂等 upsert（改 notes/content 会更新原文档而非新建）：

```python
payload = "|".join([
    doc.knowledge_type.value,
    doc.project,
    doc.equipment,
    doc.title.strip(),
    ",".join(sorted(doc.error_codes)),
])
h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
doc_id = f"{doc.knowledge_type.value}:{h}"   # 例如 alarm:9f2c…（共 24 位 hex）
```

即 ID = `<type>:<sha256(type|project|equipment|title|sorted_codes)[:24]>`。
改动 `project`/`equipment`/`title`/`error_codes` 任一项都会得到新 ID（即新文档）。

---

## 索引时分类法校验 {#taxonomy-validation}

`validate_against_taxonomy(doc, tax)` 在嵌入与写入前运行，违反即抛 `IndexingError`：

1. `doc.knowledge_type` 必须在 `tax.knowledge_types` 内；
2. 若 `doc.project` 非空，必须在 `tax.projects` 内；
3. 若 `doc.equipment` 非空，必须在 `tax.equipment` 内。

空字符串的 project/equipment 被允许（视为"未指定"），但非空的未知值会被拒绝——
这是"分类法强制"约束的落点。

---

## 主索引映射 {#es-mappings}

三个知识类型共用同一份映射（`_base_mapping`），区别只在应用层（哪些段进入 `body`）。
共用映射让跨类型检索变得简单。所有映射 `dynamic: "strict"`——未知字段在写入时被拒绝。

`index_body(dims, index_analyzer, query_analyzer)` 完整 create-index body：

```json
{
  "settings": {
    "analysis": {
      "analyzer": {
        "kb_index": {"type": "custom", "tokenizer": "ik_max_word"},
        "kb_query": {"type": "custom", "tokenizer": "ik_smart"}
      }
    }
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "knowledge_type": {"type": "keyword"},
      "project":        {"type": "keyword"},
      "equipment":      {"type": "keyword"},
      "error_codes":    {"type": "keyword"},

      "source_file":  {"type": "keyword", "index": false},
      "source_pages": {"type": "keyword", "index": false},
      "sections":     {"type": "object", "enabled": false},
      "summary":      {"type": "keyword", "index": false},

      "title": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart",
        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}
      },
      "body": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart"
      },

      "title_vec": {"type": "dense_vector", "dims": 1024, "index": true,
                    "similarity": "cosine", "index_options": {"type": "hnsw"}},
      "body_vec":  {"type": "dense_vector", "dims": 1024, "index": true,
                    "similarity": "cosine", "index_options": {"type": "hnsw"}},

      "created_at": {"type": "date"},
      "updated_at": {"type": "date"}
    }
  }
}
```

字段逐项说明：

| 字段 | ES 类型 | 关键属性 | 作用 |
|---|---|---|---|
| `knowledge_type` | keyword | — | 过滤/精确匹配 |
| `project` | keyword | — | 过滤；facet 聚合 |
| `equipment` | keyword | — | 过滤；facet 聚合 |
| `error_codes` | keyword | 多值 | 过滤（`terms`，命中任一即可）；facet 聚合 |
| `source_file` | keyword | `index: false` | 仅存储、仅展示 |
| `source_pages` | keyword | `index: false` | 仅存储、仅展示 |
| `summary` | keyword | `index: false` | ≤50 字摘要，仅展示 |
| `sections` | object | `enabled: false` | 原始段落字典，逐字返回，永不索引 |
| `title` | text | `analyzer`/`search_analyzer`；子字段 `title.keyword`（`ignore_above:256`） | BM25（带 `title^N` 加权）+ 向量；`.keyword` 可做精确排序 |
| `body` | text | `analyzer`/`search_analyzer` | BM25 召回主字段 |
| `title_vec` | dense_vector | `dims`、`index:true`、`cosine`、`hnsw` | 标题向量（当前未用于打分，预留） |
| `body_vec` | dense_vector | `dims`、`index:true`、`cosine`、`hnsw` | 向量重排与 kNN 的目标字段 |
| `created_at` / `updated_at` | date | — | 审计时间戳 |

### 分析器选择 {#analyzers}

- `_analyzer_settings()` **仅在**请求 IK 分词器（`ik_max_word` / `ik_smart`）时才输出
  `settings.analysis` 块（定义 `kb_index` / `kb_query` 两个自定义分析器）。
- 但映射里 `title`/`body` 的 `analyzer` 直接写的是传入的 `index_analyzer` /
  `query_analyzer` 名字（默认 `ik_max_word` / `ik_smart`）——IK 插件本身就注册了这两个
  分析器名。
- **无 IK 插件的回退**：把 `KB_ES__ANALYZER_INDEX` 与 `KB_ES__ANALYZER_QUERY` 都设为
  `cjk`（ES 内置 CJK 二元分词），此时不输出自定义 settings 块。
- `dims` 来自 `KB_EMBEDDING__DIMS`（默认 1024）。**两个向量字段的维度必须等于嵌入模型
  输出维度**，否则写入报维度不匹配。

### 索引命名与别名 {#index-naming}

`src/kb/es/mappings.py`：

| 函数 | 返回 | 示例（prefix=`kb`） |
|---|---|---|
| `index_name(prefix, kt, version=1)` | `{prefix}_{kt}_v{version}` | `kb_alarm_v1` |
| `alias_name(prefix, kt)` | `{prefix}_{kt}` | `kb_alarm` |
| `all_alias_pattern(prefix)` | `{prefix}_*` | `kb_*` |

物理索引带版本号（`kb_alarm_v1`），稳定别名（`kb_alarm`）指向它。reindex 时创建新版本
索引并**原子切换别名**（`src/kb/es/migrations.py` 的 `create_one` / `reindex`），实现
零停机更换映射或嵌入模型。检索时 `_index_for()`：指定 `knowledge_type` 用单个别名，
否则逗号拼接所有别名做跨类型检索。

---

## 辅助索引 {#auxiliary-indices}

两个辅助索引**不参与启动重排（reseed）**，因此跨重启持久。

### `kb_import_files` — 导入跟踪 {#kb-import-files}

`src/kb/es/import_mappings.py`。按文件内容 SHA-256 去重，并存下已提交文档的完整 ES
源载荷，供启动时 `restore_imports()` 在 reseed 清空主索引后重新写回。

`settings`: `number_of_shards: 1`、`number_of_replicas: 0`；`mappings.dynamic: "strict"`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `file_hash` | keyword | 文件内容 SHA-256，同时也是文档 `_id` |
| `file_name` | keyword | 原始文件名 |
| `file_path` | keyword | 落盘路径（供失败重试） |
| `file_size` | long | 字节数 |
| `file_type` | keyword | 扩展名 |
| `import_status` | keyword | `pending` / `committed` / `failed` |
| `committed_docs` | nested | 子字段：`_index`(keyword)、`_id`(keyword)、`_source`(object, `enabled:false`) |
| `error_message` | text | `index: false` |
| `created_at` / `updated_at` | date | 时间戳 |

### `kb_search_feedback` — 检索反馈 {#kb-search-feedback}

`src/kb/es/feedback_mappings.py`。轻量 👍/👎 信号，**纯观测**，永不回流影响检索结果。

`settings`: `number_of_shards: 1`、`number_of_replicas: 0`；`mappings.dynamic: "strict"`。

| 字段 | 类型 |
|---|---|
| `doc_id` | keyword |
| `helpful` | boolean |
| `query_text` | keyword |
| `knowledge_type` / `project` / `equipment` / `search_status` | keyword |
| `request_id` | keyword |
| `created_at` | date |

---

## 检索 API 模型 {#search-models}

`src/kb/models/search.py`。

### `SearchStatus`（枚举） {#search-status}

| 取值 | 含义 |
|---|---|
| `strict_hit` | 全部过滤 + AND 关键词命中，且在阈值内 |
| `too_many` | strict 命中数 > `strict_max_hits`，应引导用户缩小范围 |
| `loose_hit` | 退化到 OR 关键词，须带"仅供参考"横幅 |
| `vector_only` | 仅向量相似命中（低置信） |
| `no_hit` | 全部未命中 |

`SearchMode = Literal["auto", "strict", "loose", "vector_only"]`。

### `SearchRequest` {#search-request}

| 字段 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `knowledge_type` | `KnowledgeType \| None` | `None` | 为空则跨所有索引 |
| `project` | `str \| None` | `None` | `max_length=200` |
| `equipment` | `str \| None` | `None` | `max_length=200` |
| `error_codes` | `list[str]` | `[]` | 列表 `max_length=64`，每项 `max_length=64` |
| `keywords` | `list[str]` | `[]` | 列表 `max_length=64`，每项 `max_length=200` |
| `query_text` | `str \| None` | `None` | `max_length=4000`；向量召回/重排所需的原始文本 |
| `mode` | `SearchMode` | `"auto"` | 见上 |
| `size` | `int` | `10` | `1 ≤ size ≤ 50` |
| `from_` | `int` | `0` | `≥ 0` |

模型级校验：`from_ + size ≤ 10000`（`_MAX_RESULT_WINDOW`，对应 ES
`index.max_result_window`），超出在模型层返回 400 而非让 ES 报不透明错误。

### `DocHit` {#doc-hit}

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | ES `_id` |
| `score` | float | 最终打分 |
| `knowledge_type` | `KnowledgeType` | |
| `project` / `equipment` | str | |
| `error_codes` | `list[str]` | |
| `title` | str | |
| `source_file` | `str \| None` | |
| `source_pages` | `list[str]` | |
| `summary` | `str \| None` | ≤50 字 |
| `sections` | `dict[str, str]` | 原始段落，逐字、永不 AI 改写 |

### `SearchResponse` {#search-response}

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | `SearchStatus` | 见状态契约 |
| `total` | int | 命中总数 |
| `hits` | `list[DocHit]` | `too_many`/`no_hit` 时为空 |
| `effective_params` | `EffectiveParams` | 归一化后实际生效的参数（回显给上游展示） |
| `facets` | `dict[str, dict[str, int]]` | 仅 `too_many` 时填充：project/equipment/error_codes 桶计数 |
| `facets_truncated` | `dict[str, int]` | 每个 facet 落在 top 桶之外的数量（ES `sum_other_doc_count`），非零表示桶列表不完整 |
| `banner` | `str \| None` | `loose_hit`/`vector_only`/`no_hit` 时必须逐字展示 |

`EffectiveParams` 字段：`knowledge_type`、`project`、`equipment`、`error_codes`、
`keywords`。

固定横幅文案（`src/kb/services/search.py`）：

| 场景 | 文案 |
|---|---|
| `loose_hit` | 没有完全匹配的知识，以下为相关参考，仅供参考。 |
| `vector_only` | 没有关键词匹配的知识，以下基于语义相似度的相关参考，仅供参考。 |
| `no_hit` | 没有找到匹配的知识。请补充关键词或调整筛选条件后重试。 |

---

## 导入管道模型 {#ingest-models}

`src/kb/models/ingest.py`。

### 状态枚举

- `ImportStatus`：`pending` / `extracting` / `ready_for_review` / `committed` / `failed`
- `FileStatus`：`processing` / `skipped_duplicate` / `unsupported` / `failed` / `done`

### `StagedDocument`（待审文档）

预览阶段的可编辑文档，提交时转回 `KnowledgeDoc` 校验。关键字段：`index`、
`knowledge_type`、`project`、`equipment`、`title`、`error_codes`，以及各类型专有字段
（`content`/`resolution`、`procedure`/`prerequisites`、`body_text`）、`notes`、
`source_file`、`source_pages`、`raw_text_excerpt`、`confidence`(0–1)、`warnings`、
`accepted`(默认 True)。

### 其他模型

- `SkippedChunk`：`source_file`、`page_range`、`reason`(`non_content`/`no_entries`/`parse_failed`)、`hint`
- `FileInfo`：文件级状态 + 分段进度（`chunks_total`/`chunks_done`/`skipped_chunks`）
- `ImportSession`：会话整体（`session_id`、`status`、`files`、`documents`、各 `*_hint`、`created_at`）
- 请求/响应：`UploadResponse`、`ScanRequest`、`SessionResponse`、`SessionListItem`、
  `DocumentUpdate`、`AcceptReject`、`AcceptAllRequest`、`RetryRequest`、`CommitResponse`、
  `RecommitTrackingResponse`（字段细节见 [API 参考](../api-reference.md)）。

---

## 分类法模型 {#taxonomy-model}

`src/kb/models/taxonomy.py` 的 `Taxonomy`：

| 字段 | 类型 | 约束 |
|---|---|---|
| `version` | str | 任意字符串，引擎不解释；变更时建议手动 bump |
| `knowledge_types` | `list[KnowledgeType]` | |
| `projects` | `list[str]` | `min_length=1`；非空、无首尾空白、唯一 |
| `equipment` | `list[str]` | `min_length=1`；非空、无首尾空白、唯一 |

辅助方法：`has_project(p)`、`has_equipment(e)`。校验器 `_no_blanks` 拒绝空白项、带首尾
空白项与重复项。

启动时 `_sync_taxonomy_from_es()` 会聚合 ES 中实际存在的 project/equipment，把缺失值
追加回 `config/taxonomy.yaml` 并把 `version` 改写为 `auto-<时间戳>`——因此该文件在运行时
必须可写。

---

## CSV 列 → 文档字段映射 {#csv-mapping}

启动 seeder（`src/kb/services/csv_loader.py`）从 `config/` 下三个 CSV 读取文档。
列名为中文，编码 `utf-8-sig`。

### `机台报警_header.csv` → `AlarmDoc`

`项目,机台,代码,英文标题,中文标题,内容,解除流程,注意事项,ppt文件,ppt页面`

| CSV 列 | 文档字段 | 处理 |
|---|---|---|
| 项目 | `project` | strip |
| 机台 | `equipment` | strip |
| 代码 | `error_codes` | 按 `[\s,，;&、]+` 拆分为多码 |
| 中文标题 + 英文标题 | `title` | `中文（英文）`，截断 200 |
| 内容 | `content` | 空则 `—` |
| 解除流程 | `resolution` | 空则 `—` |
| 注意事项 | `notes` | |
| ppt文件 | `source_file` | |
| ppt页面 | `source_pages` | 按 `,` 拆分 |

`summary` 取 `content` 首个非空行（截断 50）。

### `机台setup_header.csv` → `SetupDoc`

`项目,设备,工站/部件/站位,规格/要求,调试步骤,调试工具,注意事项,ppt文件,PPT页面`

| CSV 列 | 文档字段 | 处理 |
|---|---|---|
| 项目 | `project` | |
| 设备 | `equipment` | |
| 工站/部件/站位 | `title` | `{设备} · {站位} 调试`（无站位则 `{设备} 调试`） |
| 规格/要求 + 调试工具 | `prerequisites` | 各成一行：`规格/要求：…` / `调试工具：…` |
| 调试步骤 | `procedure` | 空则 `—` |
| 注意事项 | `notes` | |
| ppt文件 | `source_file` | |
| PPT页面 | `source_pages` | |

### `设备经验_header.csv` → `ExperienceDoc`

`项目,机台,问题,失败描述,失败分析,根因,纠正步骤,PPT文件,PPT页面`

| CSV 列 | 文档字段 | 处理 |
|---|---|---|
| 项目 | `project` | |
| 机台 | `equipment` | |
| 问题 | `title` | 截断 200 |
| 失败描述 | `body_text` | 正文首段 |
| 失败分析 | `body_text` | 追加 `【失败分析】…` |
| 根因 | `body_text` | 追加 `【根因】…` |
| 纠正步骤 | `procedure` | |
| PPT文件 | `source_file` | |
| PPT页面 | `source_pages` | |

任一 CSV 缺失只记一条 warning 并跳过；任一行 Pydantic 校验失败也只跳过该行。

---

## 知识类型规格 schema {#type-spec}

`config/knowledge_types/<type>.yaml` 是 LLM 分段提示词与 `Doc` 模型之间的**单一事实源**
（`src/kb/services/spec.py` 加载，进程级 `lru_cache`）。三个文件全部存在才能通过加载
（否则抛错）。

顶层键：

| 键 | 类型 | 说明 |
|---|---|---|
| `type` | str | `KnowledgeType` 取值 |
| `display_name` | str | 提示词中展示的类型名 |
| `summary_zh` / `summary_en` | str | 路由提示词里的一句话简介（兼容旧 `summary`） |
| `csv_source` | str | 对应 CSV 文件路径（仅文档/示例用） |
| `fields` | list | 见下 |
| `boundary_hints` | list[str] | 条目边界提示 |
| `confidence_guide` | str | 置信度打分指南 |
| `skip_if` | list[str] | 何时返回空数组 `[]` |
| `example_input` / `example_output` | str / list[dict] | 喂给 LLM 的样例 |

`fields[]` 每项（`FieldSpec`）：`name`、`desc`、`label_zh`、`csv_column`、
`required`(bool)。`required: true` 的字段会进入分段的"必填校验"——LLM 抽取结果若必填字段
为空（`""`/`—`/`-`/`n/a` 等）或置信度 < 0.3，该条目被丢弃。

`render_segmentation_prompt(spec)` 与 `render_router_prompt(specs)` 把规格渲染成 LLM
系统提示词（详见 [文件导入管道](../architecture/import-pipeline.md) 与
[AI 对话搜索](../architecture/ai-chat.md)）。
