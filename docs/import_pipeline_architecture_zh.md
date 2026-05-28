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
[2] 知识类型检测（提供 hint 则跳过）
        │  LLM 读前 3 页 → 返回 alarm | setup | experience
        │
        ▼
[3] LLM 切分
        │  按 ingest.segmentation_chunk_chars（默认 12000）切块
        │  每块调用一次 LLM，使用类型对应的“原文复制”提示词
        │  → StagedDocument[]（index、字段、confidence、source_pages）
        │  on_chunk_progress 将 “AI analysis: i/n” 写入会话状态
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
| PDF | `pymupdf` (fitz) | 直接文本过短且页面含图片时回退 OCR |
| XLSX/XLS | `openpyxl` | 一个 sheet 一页；行序列化为 `col=val` |
| CSV | 标准库 `csv` | 自动检测编码，大文件分块为虚拟页 |
| PPTX | `python-pptx` | 每张幻灯片一页，附演讲者备注 |
| DOCX | `python-docx` | 段落聚合，表格拍平为文本 |

**OCR 回退**仅在 `ingest.ocr_enabled = true` 且直接抽取文本长度低于内部阈值时触发。PaddleOCR（`ocr_lang` 默认 `ch`）首次使用时懒加载，存在明显冷启动延迟。

可选依赖通过 `_try_import` 加载——缺失某个后端（如 PaddleOCR）不会导致服务崩溃，但相关文件会失败并给出清晰的 `ImportError`。安装额外依赖：`pip install -e ".[ingest]"`。

---

## 切分（`services/segmentation.py`）

LLM 在此扮演**结构化解析器**，不是写作者。三种系统提示（`_ALARM_SYSTEM_PROMPT`、`_SETUP_SYSTEM_PROMPT`、`_EXPERIENCE_SYSTEM_PROMPT`）要求模型：

1. 完全原文复制——禁止改写、捏造或概括。
2. 源文中缺失的字段填 `"—"`，禁止编造。
3. 输出 JSON 数组，每条记录附带 `confidence` 评分（0.0–1.0）。

文本按 `segmentation_chunk_chars` 切块，相邻块保留 `_OVERLAP_PAGES = 1` 页重叠，避免跨块边界的条目被切断。每块调用一次 LLM，结果合并。

**知识类型检测**（`detect_knowledge_type`）：当客户端未传 `knowledge_type_hint` 时，将前三页发送给 LLM 并要求三选一，结果决定使用哪个切分提示，以及最终写入哪个 ES 别名（`kb_alarm_v1` / `kb_setup_v1` / `kb_experience_v1`）。

**`project_hint` / `equipment_hint`** 会被注入切分提示，便于源文未显式提及时为文档预填项目/机台。用户仍可在预览阶段修改。

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
- **仅原文复制**：切分提示明确禁止改写。每条 segment 自带 confidence 评分，方便审阅者甄别边界条目；低置信度文档仍会出现在预览中，但应重点检查。
- **Embedding 尽力而为**：commit 时 embedding 失败不会中断写入——文档以无向量形式入库，仍可被 BM25 检索。
- **按内容哈希去重**：文件名无关；相同字节二次上传直接短路，除非显式 `force=true`。
- **导入数据可在 CSV 重 seed 后存活**：启动时主索引会被清空重建，随后 tracker 的 `committed_docs` 缓存被回放，因此导入数据能在重启间持久保留。
- **会话仅在内存**：服务重启会丢失尚未 commit 的会话。这是有意为之的简化——相比磁盘持久化部分中间状态，重新抽取的代价并不高。
