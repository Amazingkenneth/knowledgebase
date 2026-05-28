# 文件导入管道架构说明

## 概述

文件导入管道负责将任意办公文档（PDF、XLSX/XLS、CSV、PPTX、DOCX）转换为经过校验的 `KnowledgeDoc` 并写入 Elasticsearch。整个流程**强制人工审核**：LLM 仅做结构抽取，未经人工确认的暂存文档不会进入可检索的索引。

与系统其它模块一致的**零编造**原则：LLM 仅做切分与标注，必须原文复制。所有接口位于 `POST /api/v1/ingest/*`；若未配置 `KB_LLM__API_KEY` 则返回 HTTP 503。

- `POST /api/v1/ingest/upload` — multipart 上传，创建会话
- `POST /api/v1/ingest/scan` — 扫描服务端文件夹
- `GET  /api/v1/ingest/sessions[/{id}]` — 列出 / 查询会话
- `PUT  /api/v1/ingest/sessions/{id}/documents/{idx}` — 编辑暂存文档
- `PATCH /api/v1/ingest/sessions/{id}/documents/{idx}` — 接受 / 拒绝
- `POST /api/v1/ingest/sessions/{id}/commit` — 将已接受文档写入 ES

---

## 端到端流程

```
客户端（文件或文件夹路径 + 可选 hints）
        │
        ▼
[0] 哈希与去重
        │  对文件字节计算 SHA-256 → 查询 kb_import_files 索引
        │  已经 committed → SKIPPED_DUPLICATE（除非 force=true）
        │  否则 → tracker 记录 pending，将文件写入 upload_dir
        │
        ▼
[1] 文本抽取（按文件类型）
        │  PDF: pymupdf 直接抽取 → 图片型页面回退 PaddleOCR
        │  XLSX/XLS: openpyxl，每个 sheet 一“页”
        │  CSV: 标准库 csv，按行分块为虚拟页
        │  PPTX: python-pptx，每张幻灯片一页
        │  DOCX: python-docx，段落聚合为页
        │  → list[(页码, 文本)]
        │
        ▼
[2] 按块路由（提供 knowledge_type_hint 时锁定单一类型，跳过路由）
        │  按 ingest.segmentation_chunk_chars（默认 12000）切块
        │  每块由 LLM 路由器返回**所含类型列表**：
        │      {"types": ["alarm", "setup"]}        ← 混合块
        │      {"types": ["experience"]}            ← 单一类型
        │      {"types": ["skip"]}                  ← 非正文（封面 / 目录 / 前言）
        │  skip → 丢弃该块并生成友好的 SkippedChunk（reason、hint）
        │
        ▼
[3] LLM 切分（同一块对每个检测到的类型各调用一次）
        │  提示词由 config/knowledge_types/<type>.yaml 渲染——是 LLM 契约
        │    与 pydantic 模型的唯一来源
        │  每次按类型的调用都带"忽略其他类型内容"的规则，避免混合块
        │    被错误塞入不匹配的 schema
        │  超长单页按结构（标题/段落/换行）继续细分
        │  相邻块保留 1 页重叠；按知识类型在切分后做去重
        │  JSON 解析失败时：抢救最长合法前缀 → 触发修复重试 →
        │                   按页二分递归（递归下限：1 页）
        │  → (StagedDocument[]，SkippedChunk[])
        │  on_chunk_progress 将 "AI analysis: i/n" 写入会话状态
        │
        ▼
[4] 会话进入 READY 状态
        │  ImportSession.documents 已填充；status = ready_for_review
        │
        ▼ （客户端审阅 / 编辑 / 接受拒绝）
        │
[5] POST /commit
        │  对每个 accepted=True 的 StagedDocument：
        │    → _staged_to_knowledge_doc(): 转为 Alarm/Setup/ExperienceDoc
        │    → validate_against_taxonomy()
        │    → 通过 DashScope embed [title_text, body_text]（尽力而为）
        │    → 以 refresh="wait_for" 写入 kb_<type>_v1 别名
        │    → 按 file_hash 聚合，供 tracker 更新
        │  record_committed(file_hash, [es_actions])
        │  → CommitResponse {committed, skipped, errors}
```

步骤 1–4 在后台 `asyncio.create_task` 中执行；upload/scan 接口立即返回 `202 Accepted` 与 `session_id`。客户端轮询 `GET /sessions/{id}`（携带 `files_processed`、单文件 `status`/`message` 及会话级 `message`）直到 `status == ready_for_review`。

---

## 文本抽取（`services/extraction.py`）

每种文件类型都有专门抽取函数，返回 `list[PageText] = list[(int, str)]`。页码全程保留，切分得到的文档可通过 `source_pages` 回溯到源文件页码。

| 类型 | 后端 | 说明 |
|---|---|---|
| PDF | `pymupdf` (fitz) | `page.get_text` 抽取正文 + `page.find_tables()` 将表格渲染为竖线网格；图片型页面回退 PaddleOCR |
| XLSX/XLS | `openpyxl` | 一个 sheet 一页；行渲染为 `\| col \| col \|` 竖线网格；顶部标注 sheet 名称 |
| CSV | 标准库 `csv` | 自动检测编码（utf-8-sig / utf-8 / gb18030 / latin-1）；行以 tab 拼接 |
| PPTX | `python-pptx` | 每张幻灯片一页；表格渲染为竖线网格；附演讲者备注 |
| DOCX | `python-docx` | 段落聚合；表格渲染为竖线网格 |

**表格感知**：PDF、DOCX、PPTX、XLSX 的表格统一以 `| 单元格 | 单元格 | 单元格 |` 形式渲染，使列/行关系在送入 LLM 时得以保留——无论横表（表头在上）还是竖表（表头在左），都按底层库返回的布局原样呈现。对 PDF 来说，`get_text` 的扁平 token 视图与表格网格视图**同时**送给 LLM。单元格中嵌入的 `|` 会替换为 `/` 以避免破坏网格结构。

**PDF 文本清洗**：`_clean_extracted_text` 会剥离 NUL、软连字符（`\xad`）、BOM、换页符及其它常见的 C0 控制字符——这些都是 PDF 抽取器经常漏出的"隐形字符"；同时统一 Windows/Mac 换行符，将 3 行以上的空行折叠为 2 行。这意味着下游切分与 ES 索引无需再防御这些可能破坏检索或 JSON 解析的字符。

**OCR 回退**仅在 `ingest.ocr_enabled = true` 且页面直接文本过短（或可打印字符比例偏低）且包含图片时触发。PaddleOCR（`ocr_lang` 默认 `ch`）首次使用时懒加载，存在明显冷启动延迟。OCR 结果**仅在**显著更长（>20%）**且**通过可打印字符健全性检测后，才会替换直接文本——避免 OCR 噪声覆盖原本质量良好的抽取文本。OCR 异常会被捕获并记录日志，默认保留直接文本。

可选依赖通过 `_try_import` 加载——缺失某个后端（如 PaddleOCR）不会导致服务崩溃，但相关文件会失败并给出清晰的 `ImportError`。安装额外依赖：`pip install -e ".[ingest]"`。

---

## 知识类型规范（`config/knowledge_types/*.yaml`）

每种知识类型都有一个 YAML 规范文件，**同时驱动 LLM 提示词和存储契约**。修改 YAML 会同步改变 LLM 被告知要提取的内容**以及**与 pydantic 模型的对齐校验——两者不会再出现漂移。

```
config/knowledge_types/
├── alarm.yaml        ← 对应 config/机台报警_header.csv
├── setup.yaml        ← 对应 config/机台setup_header.csv
└── experience.yaml   ← 对应 config/设备经验_header.csv
```

每个规范文件包含：

| 块 | 用途 |
|---|---|
| `summary_zh` / `summary_en` | 一句话描述，写入路由提示词供 LLM 判断是否选用此类型 |
| `fields[]` | 输出 JSON 结构——每个字段含 `name`、`desc`，可选 `label_zh`、`csv_column` |
| `boundary_hints[]` | 切分条目时的判定线索 |
| `skip_if[]` | "非正文"判定规则（封面、目录、前言…） |
| `confidence_guide` | 单条 `confidence` 评分准则 |
| `example_input` / `example_output` | 取自 CSV 第一行的 few-shot 范例 |

`services/spec.py` 负责加载并缓存这些 YAML，然后渲染两类提示词：

- **`render_segmentation_prompt(spec)`** — 渲染按类型的抽取提示词。包含字段列表（向 LLM 展示中文标签与 CSV 列名）、范例，以及一条显式规则：*"仅提取 `<type>` 类型条目；同块中其他类型的内容请直接忽略。"* 正是这条规则使同一块可以被多个抽取器安全处理而不产生交叉污染。
- **`render_router_prompt(specs)`** — 渲染路由提示词。返回 `{"types": [...]}`（列表），使得同时包含报警和其调试步骤的块会被路由到**多个**抽取器。

对齐校验测试（`tests/unit/test_spec.py`）确保 pydantic 模型的每个必需字段都被规范覆盖，且规范中的 `example_output` 能完整通过 `_parsed_to_staged()` 流程——提示词与模型的漂移在测试阶段就会暴露，而不是等到提交时才报错。

---

## 切分（`services/segmentation.py`）

LLM 在此扮演**结构化解析器**，不是写作者。按类型的系统提示词由上述 YAML 规范渲染得到，要求模型：

1. 完全原文复制——禁止改写、捏造或概括。
2. 源文中缺失的字段填 `"—"`，禁止编造。
3. 将 `| col | col | col |` 形式的行识别为表格行，保留单元格顺序。
4. 输出 JSON 数组，每条记录附带 `confidence` 评分（0.0–1.0）。
5. **只提取**当前提示词指定的类型；同块中其他类型的内容必须忽略。

### 切块

`chunk_pages()` 将页打包为受 `segmentation_chunk_chars` 约束的块，相邻块保留 `_OVERLAP_PAGES = 1` 页重叠，确保跨块边界的条目仍能被完整看到。

打包前，`_split_oversized_page()` 会对任何单页超过 `max_chars` 的页进行**结构化细分**，优先级如下：

1. **类标题边界** — markdown 标题、中文 `第N章/节`、英文 `Chapter N`、编号小节（`1.2.3 …`）、纯大写行。
2. **段落分隔**（`\n\n`）。
3. **行分隔**（`\n`）。
4. **硬字符截断**（最后手段，仅在单行长度超过 `max_chars` 时使用）。

细分得到的子页**保留原页码**，因此 `source_pages` 的可追溯性不受影响。这堵上了之前一个隐性漏洞——单个超大页（如 1 页的 DOCX、巨大的 sheet、长版 PDF 页）会越过 LLM 输入预算被悄悄送出。

### JSON 健壮性

LLM 的失败形式多样：输出被截断（中途撞上 `max_tokens`）、从噪声 PDF 复制了非法控制字符、加上 "Here is the JSON:" 之类的前言，或包了 markdown 围栏。`_parse_json_array()` 全部处理：

1. 剥离 markdown 围栏，清洗 C0 控制字符。
2. 直接尝试 `json.loads`。
3. 失败后从 `[` 开始扫描，按括号/引号深度寻找**最长合法前缀**——即便响应在某条记录中途被截断，也能恢复已完成的条目。
4. 将单个对象提升为只含一个元素的数组。

如果某块仍解析失败，`_segment_chunk_with_fallback()` 启用两层恢复机制：

- **修复重试**（每块至多一次）：将 LLM 自己的坏输出回传给它，要求重新输出合法 JSON。
- **按页二分恢复**：将失败块在页边界一分为二，每半重新切分（递归下限：单页，此时干净放弃胜过反复折腾）。这就是应对"单条记录超过 `max_tokens`"的方案——块会持续缩小，直到该条目能装下。

LLM 的网络/HTTP 异常同样会触发二分恢复，而不是直接丢弃整块。

### 跨块去重

`_deduplicate_entries()` 折叠由 1 页重叠产生的重复条目，按知识类型使用不同 key：

| 类型 | 去重 key | 冲突保留规则 |
|---|---|---|
| ALARM | 归一化的 `error_code` | `confidence` 较高者胜 |
| SETUP | 归一化的 `station` + `procedure` 前 80 字符 | `confidence` 较高者胜 |
| EXPERIENCE | 归一化的 `problem` + `failure_desc` 前 80 字符 | `confidence` 较高者胜 |

key 为空的条目**原样保留**，不会被相互折叠——边界条目交给人工复核，胜过被静默合并。

### 按块多类型路由

`classify_chunk_types()` 对每一块独立分类，返回**所含类型的列表**：

- `[]`（路由器返回 `skip`）→ 丢弃该块；生成 `SkippedChunk(reason="non_content")` 带友好提示。
- `[KnowledgeType.ALARM]` → 一次抽取调用，使用 alarm 提示词。
- `[KnowledgeType.ALARM, KnowledgeType.SETUP]` → 对**同一块文本**调用两次抽取器；由于提示词显式要求忽略其他类型内容，两边各自只产出自己类型的条目。

当客户端在上传时传入 `knowledge_type_hint`，该 hint 会**锁定**所有块为该类型，路由器被完全跳过——适用于明确知晓整份文件类型、且希望节省分类调用开销的场景。

`detect_knowledge_type()` 作为对外的"主导类型"便捷接口保留（如 UI 提示用），其实现就是取 `classify_chunk_types` 的第一项。

### 非正文处理（封面、目录、前言）

非正文页面（封面、目录、前言、修订记录、术语表、索引、版权页，或与具体条目无关的纯散文）会被路由器按各 spec 的 `skip_if[]` 规则识别并在切分前丢弃。它们以 `FileInfo.skipped_chunks: list[SkippedChunk]` 形式返回给 UI，每条包含：

- `page_range` — 被跳过的页码范围
- `reason` — `non_content` | `no_entries` | `low_confidence`
- `hint` — 一句话说明，便于审阅者直接处理

文件卡片的 `message` 也会做汇总，例如：*"Extracted 14 documents. 2 non-content page(s) skipped (covers/TOC/preface); 1 low-confidence page(s) — please review."*

### 保真校验（反捏造）

切分完成后，每个需要原文复制的字段（`content`、`resolution`、`procedure`、`failure_desc`）都会调用 `verify_extraction_fidelity()` 与源文本比对：先严格匹配**当前块文本**，再回退匹配**整篇文件文本**（处理跨块边界的合法内容）。校验失败时字段仍保留，但暂存文档会带上 `fabrication_warning: <field>` 标记供审阅。

### Hint 与超时

`project_hint` / `equipment_hint` 会被注入切分提示，便于源文未显式提及时为文档预填项目/机台。用户仍可在预览阶段修改。

`_estimate_timeout()` 根据实际 payload 大小估算 HTTP 读超时，使用 CJK 感知的 token 估算（中文 ≈ 1.5 tok/字符，拉丁 ≈ 0.25 tok/字符）。超长块不会触发超时——它们会先撞上 `max_tokens` 上限，再走二分恢复路径。

---

## 会话状态与审核

```python
class ImportSession:
    session_id: str          # uuid4
    status: ImportStatus     # extracting | ready_for_review | committed | failed
    files: list[FileInfo]    # 单文件抽取状态
    documents: list[StagedDocument]
    ...hints, created_at
```

会话**仅存储在内存**中（`ImportPipeline._sessions: dict[str, ImportSession]`）。服务重启会丢失所有进行中的会话，用户需重新上传。已 commit 的文件不受影响——它们保存在 ES 中，下次启动会自动恢复（见 Tracker 章节）。

`StagedDocument` 以并集方式承载所有类型字段（alarm 的 `content`/`resolution`，setup 的 `procedure`/`prerequisites`，experience 的 `body_text`）。`accepted` 默认 `True`，客户端通过 PATCH 接口切换。字段编辑走 PUT，直接修改对象——不保留修改历史。

---

## Commit 流程（`commit_session`）

对每个 `accepted=True` 的 `StagedDocument`：

1. `_staged_to_knowledge_doc` 根据类型构建子类（`AlarmDoc` / `SetupDoc` / `ExperienceDoc`）。缺失的必填字符串回退为 `"—"`；setup 缺少标题时回退为 `f"{equipment} 调试"`。
2. `validate_against_taxonomy` 拒绝未知的 `project` / `equipment`——这些会表现为 `string_too_short` 或校验错误，并被聚合到 `errors` 列表。
3. `EmbeddingClient.embed([title_text, body_text])` 采用**尽力而为**策略：任何失败都仅记 warning，文档以 `null` 向量入库（BM25 仍可用，向量重排会静默忽略）。
4. 以 `refresh="wait_for"` 调用 `es.index(...)` 写入对应别名，`_id = doc_id(doc)` 为稳定哈希，重复 commit 幂等。
5. ES 写入动作（`{_index, _id, _source}`）按源文件 `file_hash` 分组收集。

循环结束后，`record_committed(file_hash, actions)` 更新 tracker。校验/写入失败会**中断循环**（`break`）——这样不会留下部分提交的不一致状态，用户修复后可重新 commit。

**友好的 commit 错误**。`_friendly_validation_message()` 将原始 pydantic 错误转成一句指向具体字段的可操作提示，例如：*"'resolution' is empty. Required for alarms — paste the Remedy / 解除流程 section."* `CommitResponse.errors[]` 中每一项同时携带 `error`（错误信息）与 `hint`（如何处理）。Taxonomy / ES 写入失败也会得到对应的提示——*"Check that the project/equipment values match config/taxonomy.yaml."*

---

## 文件追踪器（`kb_import_files` 索引）

Tracker 承担两项职责：**去重**与**自动恢复**。

**去重**：以文件字节的 SHA-256 为 key。`start_upload` 在持久化前调用 `tracker.exists(hash)`，若已有 `committed` 记录且未传 `force=true`，文件被标记为 `SKIPPED_DUPLICATE`。

**自动恢复**：每个已 commit 文档的完整 ES source 被存入 tracker 记录的 `committed_docs[]`。启动时 `seed` 会先用 CSV 清空并重建主索引；随后 `restore_imports()`（位于 `services/seed.py`）调用 `tracker.get_all_committed()` 并批量重新写回对应别名。这正是导入文档能在“启动即重新 seed”机制下幸存的原因——tracker（而非源文件）是导入数据的事实来源。

Tracker 记录的生命周期状态：

| `import_status` | 设置位置 | 含义 |
|---|---|---|
| `pending` | 上传时由 `record_pending()` 写入 | 文件已落盘，等待抽取 |
| `committed` | commit 后由 `record_committed()` 写入 | 已接受文档全部入库，payload 已缓存用于恢复 |
| `failed` | 抽取失败时由 `record_failed()` 写入 | 错误信息已记录，不会自动恢复 |

---

## 配置项

全部位于 `config/settings.yaml` 的 `ingest:` 段，或对应 `KB_INGEST__*` 环境变量。

| 参数 | 默认值 | 作用 |
|---|---|---|
| `ingest.upload_dir` | `data/uploads` | 上传文件落盘目录（命名为 `<hash>_<name>`） |
| `ingest.max_file_size_mb` | `50` | 单文件大小上限，超限标记 FAILED |
| `ingest.allowed_extensions` | `pdf, xlsx, xls, csv, pptx, docx` | 其它扩展名标记 UNSUPPORTED |
| `ingest.ocr_enabled` | `true` | 关闭后 PDF 图片页将返回空文本 |
| `ingest.ocr_lang` | `ch` | PaddleOCR 语言包 |
| `ingest.segmentation_max_tokens` | `8000` | 切分 LLM 调用的最大 token 数 |
| `ingest.segmentation_chunk_chars` | `12000` | 单次喂给切分器的字符数 |
| `ingest.session_ttl_minutes` | `120` | （预留）会话保留时长 |

---

## 关键设计约束

- **强制人工审核**：未经显式 commit，文档绝不进入检索索引。即便是“快速通道”（扫描整个文件夹）也止步于 `ready_for_review`。
- **规范驱动**：每种知识类型在 `config/knowledge_types/<type>.yaml` 中只定义一次。LLM 提示词、范例、跳过规则与对齐校验都从该文件读取，不存在第二份字段清单。
- **支持混合类型文件**：路由按块进行，不按文件。同时包含报警与调试流程的文档无需手动拆分即可正确切分。
- **仅原文复制**：切分提示明确禁止改写。每条 segment 自带 confidence 评分，方便审阅者甄别边界条目；低置信度文档仍会出现在预览中，但应重点检查。
- **友好反馈**：被跳过的块与提交错误均附带可操作的 `hint`，无需查看服务端日志即可定位修复。
- **Embedding 尽力而为**：commit 时 embedding 失败不会中断写入——文档以无向量形式入库，仍可被 BM25 检索。
- **按内容哈希去重**：文件名无关；相同字节二次上传直接短路，除非显式 `force=true`。
- **导入数据可在 CSV 重 seed 后存活**：启动时主索引会被清空重建，随后 tracker 的 `committed_docs` 缓存被回放，因此导入数据能在重启间持久保留。
- **会话仅在内存**：服务重启会丢失尚未 commit 的会话。这是有意为之的简化——相比磁盘持久化部分中间状态，重新抽取的代价并不高。
