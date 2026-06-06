# API 参考

所有接口在 `http://<host>:8000` 下提供。交互式、始终最新的 schema 由 FastAPI 生成，
是**权威事实来源**（永远不会与代码漂移）：

- **Swagger UI** —— `/docs`
- **ReDoc** —— `/redoc`
- **OpenAPI JSON** —— `/openapi.json`

本页是人工整理、自洽完整的参考：下面每个接口都附带具体的请求体、响应体以及可能的状态码 ——
足以在不阅读源码的情况下完成集成。模型定义位于 `src/kb/models/`，各接口处理函数位于
`src/kb/api/`。

## 约定 {#conventions}

- **基础路径** —— 所有业务接口都在 `/api/v1` 下。元接口（`/healthz`、`/readyz`、`/metrics`、
  `/docs`）位于根路径。
- **内容类型** —— 除特别说明外，请求与响应体均为 `application/json`（文件上传为
  `multipart/form-data`）。
- **校验** —— 请求体由 pydantic 校验；非法请求体返回 **422** 及字段级错误列表（FastAPI 默认
  格式）。下表不再重复列出这类字段级 422。
- **无鉴权** —— 服务本身不带认证/授权；请部署在网关或反向代理之后由其控制访问（见
  [安全](operations/security.zh.md)）。
- **功能开关** —— chat、extract、ingest 需要 `KB_LLM__API_KEY`，否则返回 **503**；未设置
  `KB_EMBEDDING__API_KEY` 时向量排序降级为 BM25（搜索仍返回 **200**）。见
  [配置](configuration.zh.md)。

---

## 搜索 {#search}

### `POST /api/v1/search`

结构化混合检索（`src/kb/api/search.py` → `src/kb/services/search.py`）。请求体是**已抽取好的**
`SearchRequest` —— 本接口**不**解析自然语言（那是 `/extract` 与 `/chat` 的职责）。

**请求 —— `SearchRequest`**

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `knowledge_type` | `alarm\|setup\|experience\|null` | `null` | `null` 表示检索全部类型 |
| `project` | `str\|null`（≤200） | `null` | 精确匹配过滤（taxonomy 值） |
| `equipment` | `str\|null`（≤200） | `null` | 精确匹配过滤（taxonomy 值） |
| `error_codes` | `list[str]`（≤64 项，每项 ≤64 字符） | `[]` | 文档含**任一**即命中 |
| `keywords` | `list[str]`（≤64 项，每项 ≤200 字符） | `[]` | BM25 关键词 |
| `query_text` | `str\|null`（≤4000） | `null` | 向量检索阶段的原始文本 |
| `mode` | `auto\|strict\|loose\|vector_only` | `auto` | 召回级联选择 |
| `size` | `int`（1–50） | `10` | 每页条数 |
| `from_` | `int`（≥0） | `0` | 偏移；`from_ + size` 必须 ≤ `10000` |

```json
{
  "knowledge_type": "alarm",
  "project": "PDX",
  "equipment": "Aligner",
  "error_codes": ["E-1234"],
  "keywords": ["真空", "泄漏"],
  "query_text": "真空泄漏报警如何处理",
  "mode": "auto",
  "size": 5,
  "from_": 0
}
```

**响应 —— `SearchResponse`**

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | `SearchStatus` | `strict_hit\|too_many\|loose_hit\|vector_only\|no_hit` 之一 |
| `total` | `int` | 命中阶段的总匹配数 |
| `hits` | `list[DocHit]` | 逐字返回的文档（见下） |
| `effective_params` | `EffectiveParams` | 实际生效的过滤/关键词回显 |
| `facets` | `dict[str, dict[str,int]]` | 仅在 `too_many` 时出现 —— 各分面 值→计数 |
| `facets_truncated` | `dict[str,int]` | 超出已展示桶的各分面值计数 |
| `banner` | `str\|null` | 在 `loose_hit`/`vector_only`/`no_hit` 时**必须原样展示** |

`DocHit` 为 `{ id, score, knowledge_type, project, equipment, error_codes[], title,
source_file?, source_pages[], summary?, sections{} }`。`sections` 逐字保存原始内容字段 ——
绝不经 AI 改写。

```json
{
  "status": "strict_hit",
  "total": 2,
  "hits": [
    {
      "id": "kb_alarm_v1:9f2c…",
      "score": 14.21,
      "knowledge_type": "alarm",
      "project": "PDX",
      "equipment": "Aligner",
      "error_codes": ["E-1234"],
      "title": "真空泄漏报警（Vacuum Leak）",
      "source_file": "alarms_2024.pptx",
      "source_pages": ["12"],
      "summary": "真空泄漏报警处理",
      "sections": {
        "content": "报警触发条件……",
        "resolution": "1. 检查密封圈……",
        "notes": "复位前确认……"
      }
    }
  ],
  "effective_params": {
    "knowledge_type": "alarm",
    "project": "PDX",
    "equipment": "Aligner",
    "error_codes": ["E-1234"],
    "keywords": ["真空", "泄漏"]
  },
  "facets": {},
  "facets_truncated": {},
  "banner": null
}
```

`status` 取值是跨系统契约 —— 各值的含义及调用方如何渲染见
[状态契约](architecture/search-ranking.zh.md#status-contract)。召回级联与 BM25 + 向量混合公式见
[检索与排序](architecture/search-ranking.zh.md)。

**状态码**

| 码 | 触发条件 |
|---|---|
| `200` | 任何成功检索（含 `no_hit`） |
| `422` | `from_ + size > 10000`，或任一字段越界 |

---

## 对话与抽取 {#chat}

二者均需要 `KB_LLM__API_KEY`（否则 **503**）。完整设计见
[AI 对话搜索](architecture/ai-chat.zh.md)。

### `POST /api/v1/chat`

对话式搜索。服务端**无状态** —— 客户端每轮发送完整消息历史。内部流程：摘要早期对话 →
抽取参数 → 检索 → 基于结果作答。

**请求 —— `ChatRequest`**

| 字段 | 类型 | 说明 |
|---|---|---|
| `messages` | `list[{role, content}]`（1–200 条，`content` ≤20 000 字符） | 完整对话，按旧→新 |
| `last_search_params` | `dict\|null` | 回传上一轮响应的 `effective_params` 以启用**更新模式**（参数增量修改） |

```json
{
  "messages": [
    {"role": "user", "content": "PDX 的 Aligner 真空泄漏报警怎么处理？"}
  ],
  "last_search_params": {
    "project": "PDX", "equipment": "Aligner",
    "knowledge_type": "alarm", "error_codes": [], "keywords": ["真空", "泄漏"]
  }
}
```

**响应 —— `ChatResponse`**

| 字段 | 类型 | 说明 |
|---|---|---|
| `content` | `str` | Markdown 回答，仅基于检索到的文档 |
| `search_results` | `list[DocHit]\|null` | 用作上下文的命中（未检索时为 null） |
| `search_status` | `SearchStatus\|null` | 内部检索状态 |
| `effective_params` | `EffectiveParams\|null` | 下一轮作为 `last_search_params` 回传 |
| `search_error` | `bool` | 检索**失败**（如 ES 不可达）时为 `true` —— 区别于真正的 `no_hit`；应提示*重试*而非"未找到知识" |

```json
{
  "content": "根据知识库中的 1 条文档……\n\n1. 检查密封圈……",
  "search_results": [ { "id": "kb_alarm_v1:9f2c…", "title": "真空泄漏报警（Vacuum Leak）", "...": "..." } ],
  "search_status": "strict_hit",
  "effective_params": {
    "knowledge_type": "alarm", "project": "PDX", "equipment": "Aligner",
    "error_codes": [], "keywords": ["真空", "泄漏"]
  },
  "search_error": false
}
```

**状态码**

| 码 | 触发条件 |
|---|---|
| `200` | 产生回答（即使 `no_hit` 或 `search_error: true`） |
| `502` | LLM 上游报错或返回无法解析的内容 |
| `503` | 未设置 `KB_LLM__API_KEY` |

### `POST /api/v1/extract`

自然语言 → 结构化参数。LLM 以实时 taxonomy 引导，任何不在 taxonomy 中的值会被置为 `null`
（未知过滤值会悄无声息地零命中）。

**请求 —— `ExtractRequest`**：`{ "query": "PDX aligner 真空泄漏 E-1234" }`（1–20 000 字符）。

**响应 —— `ExtractResponse`**

```json
{
  "project": "PDX",
  "knowledge_type": "alarm",
  "error_codes": ["E-1234"],
  "equipment": "Aligner",
  "keywords": ["真空", "泄漏"],
  "is_sentence": false
}
```

`is_sentence` 在自然语言问句时为 `true`，关键词组合时为 `false` —— 调用方可据此决定是否同时
向 `/search` 传入 `query_text`。

**状态码：** `200` 成功 · `502` LLM 输出无法解析 · `503` 无 LLM 密钥。

---

## 文档 {#documents}

直接对类型索引做 CRUD（`src/kb/api/documents.py`）。`{knowledge_type}` 按枚举校验；请求体按类型
判别为对应的 `AlarmDoc`/`SetupDoc`/`ExperienceDoc` 模型并对照 taxonomy 校验。各类型字段 schema
见[数据模型](reference/data-model.zh.md)。

| 方法与路径 | 用途 | 成功 | 错误 |
|---|---|---|---|
| `GET /api/v1/documents/stats` | 按类型/项目/设备/报警码聚合计数（首页用） | `200` | — |
| `POST /api/v1/documents/{knowledge_type}` | 索引单条文档 | `201 {id}` | `400` 文档/taxonomy 非法 |
| `POST /api/v1/documents/{knowledge_type}/_bulk` | 批量索引（数组请求体） | `200 {indexed, errors[]}` | 解析错误以 `indexed:0` 返回 |
| `DELETE /api/v1/documents/{knowledge_type}/{doc_id}` | 按 id 删除 | `204` | 不存在则 `404` |

`POST …/{knowledge_type}` 请求体（alarm 示例）与响应：

```json
// 请求
{
  "project": "PDX", "equipment": "Aligner",
  "title": "真空泄漏报警（Vacuum Leak）", "error_codes": ["E-1234"],
  "content": "报警触发条件……", "resolution": "1. 检查密封圈……", "notes": ""
}
// 201 响应
{ "id": "kb_alarm_v1:9f2c…" }
```

`GET /api/v1/documents/stats` 返回：

```json
{
  "total": 412,
  "by_type": { "alarm": 210, "setup": 120, "experience": 82 },
  "by_project": { "PDX": 96, "MEM": 88, "…": 0 },
  "by_equipment": { "Aligner": 54, "Pump": 40 },
  "by_error_code": { "E-1234": 7 }
}
```

`_bulk` 接口**先校验所有行**；若任一行解析失败，返回
`{ "indexed": 0, "errors": [{"row": 3, "error": "…"}] }` 且不索引任何内容（解析阶段全或无）。
能解析但在索引时失败的行会带着该批次的逐行结果出现在 `errors` 中。

---

## 文件导入 {#ingest}

带审核的 文件 → 文档 管道（`src/kb/api/ingest.py`）。需要 `KB_LLM__API_KEY`（切分用 LLM）。完整
设计见[文件导入管道](architecture/import-pipeline.zh.md)。所有处理均为**异步**：upload/scan/retry 立即
返回 `202` 与 `session_id`，随后需**轮询**会话直到状态变为 `ready_for_review`。

### 接口

| 方法与路径 | 用途 | 成功 |
|---|---|---|
| `POST /api/v1/ingest/upload` | 多文件 multipart 上传 | `202` `UploadResponse` |
| `POST /api/v1/ingest/scan` | 扫描 `ingest.scan_root` 下的服务端文件夹 | `202` `UploadResponse` |
| `GET /api/v1/ingest/sessions?limit=20` | 列出最近会话 | `200` `SessionListItem[]` |
| `GET /api/v1/ingest/sessions/{id}` | 查看会话（轮询此接口） | `200` `SessionResponse` |
| `GET /api/v1/ingest/sessions/{id}/summary` | 提交前的后果计数 | `200` `CommitSummary` |
| `PUT /api/v1/ingest/sessions/{id}/documents/{idx}` | 编辑暂存文档（部分） | `200 {status:"updated"}` |
| `PATCH /api/v1/ingest/sessions/{id}/documents/{idx}` | 接受/拒绝单条暂存文档 | `200 {status:"updated"}` |
| `PATCH /api/v1/ingest/sessions/{id}/documents/{idx}/resolve` | 解决冲突（保留 / 覆盖 / 合并） | `200 {status:"resolved"}` |
| `POST /api/v1/ingest/sessions/{id}/documents/accept-all` | 全部接受（可限定某类型） | `200 {accepted:N}` |
| `POST /api/v1/ingest/sessions/{id}/files/{file_hash}/retry` | 重处理单个失败文件 | `202` `UploadResponse` |
| `POST /api/v1/ingest/sessions/{id}/retry-failed` | 重处理**全部**失败文件 | `202` `UploadResponse` |
| `POST /api/v1/ingest/sessions/{id}/commit` | 将已接受文档写入 ES + 追踪索引 | `200` `CommitResponse` |
| `POST /api/v1/ingest/sessions/{id}/recommit-tracking` | 持久化恢复：重试失败的追踪写入 | `200` `RecommitTrackingResponse` |

### 上传 / 扫描

`POST /upload` 为 `multipart/form-data`：一个或多个 `files`，外加可选表单字段
`knowledge_type_hint`、`project_hint`、`equipment_hint` 和 `force`（重新导入哈希已提交的文件）。
`POST /scan` 接收 JSON `ScanRequest`：

```json
{
  "folder_path": "incoming/2024-06",
  "recursive": false,
  "knowledge_type_hint": "alarm",
  "project_hint": "PDX",
  "equipment_hint": "Aligner",
  "force": false
}
```

`folder_path` 在 **`ingest.scan_root` 之下**解析；逃逸该根目录的路径返回 **400**（见
[安全](operations/security.zh.md#scan-boundary)）。二者均返回 `UploadResponse`：

```json
{
  "session_id": "8b1f…",
  "files": [
    { "file_name": "alarms.pptx", "file_hash": "f3a…", "file_type": "pptx",
      "file_size": 184320, "status": "processing", "message": "",
      "chunks_total": null, "chunks_done": null, "skipped_chunks": [] }
  ]
}
```

### 轮询会话

`GET /sessions/{id}` 返回 `SessionResponse`。轮询直到 `status` 为 `ready_for_review`（终态：
`committed`、`failed`）：

```json
{
  "session_id": "8b1f…",
  "status": "ready_for_review",
  "message": "",
  "files_total": 1,
  "files_processed": 1,
  "files": [
    { "file_name": "alarms.pptx", "file_hash": "f3a…", "file_type": "pptx",
      "status": "done", "chunks_total": 6, "chunks_done": 6,
      "skipped_chunks": [
        { "source_file": "alarms.pptx", "page_range": "1", "reason": "non_content",
          "hint": "封面页，无知识内容" }
      ] }
  ],
  "documents": [
    { "index": 0, "knowledge_type": "alarm", "project": "PDX", "equipment": "Aligner",
      "title": "真空泄漏报警", "error_codes": ["E-1234"],
      "content": "……", "resolution": "……", "notes": "",
      "source_file": "alarms.pptx", "source_pages": ["12"],
      "raw_text_excerpt": "……", "confidence": 0.82, "warnings": [], "accepted": true,
      "collision": null, "collision_action": null, "related": [],
      "dup_group_id": null, "dup_primary": true }
  ]
}
```

每条暂存文档还可能携带分段后新增的富化字段（见下文[冲突与交叉引用](#ingest-conflicts)）：

- `collision` —— 当提交该文档**会覆盖**身份相同的已有 KB 文档时，设为一个
  `ExistingDocSnapshot`，否则为 `null`。`collision_action`（`null` | `keep` | `overwrite`
  | `merge`）是审阅者的决定 —— 当 `collision` 已设置且 `collision_action` 为 `null` 时，该文档
  **被阻断提交**。
- `related[]` —— 相关已提交文档（`RelatedDoc`：`doc_id`、`knowledge_type`、`title`、
  `equipment`、`error_codes`、`match_reason` ∈ {`error_code`、`equipment`、`similar`}、
  `snippet`）。
- `dup_group_id` / `dup_primary` —— 批内近重复分组；变体共享 `dup_group_id`，仅
  `dup_primary` 的那条默认 `accepted`。

`ImportStatus` 取值：`pending` · `extracting` · `ready_for_review` · `committed` ·
`failed`。`FileStatus` 取值：`processing` · `skipped_duplicate` · `unsupported` ·
`failed` · `done`。切分期间 `chunks_done` 统计的是**已完成**的块，因此只有当分析全部结束后文件才会读到 `chunks_done == chunks_total`；其后短暂的去重/组装阶段会以 `Finalizing…` 消息上报。

`status: skipped_duplicate` 的文件还会额外带一个 `duplicate_info` 对象，描述 KB 已为该内容保存了什么，便于 UI 解释这次跳过：

```json
{ "file_name": "alarms.pptx", "file_hash": "f3a…", "file_type": "pptx",
  "status": "skipped_duplicate", "message": "Already imported on 2024-06-12T…",
  "duplicate_info": {
    "imported_at": "2024-06-12T08:31:00+00:00",
    "original_file_name": "alarms-v1.pptx",
    "doc_count": 14,
    "documents": [
      { "knowledge_type": "alarm", "title": "真空泄漏报警", "error_codes": ["E-1234"] }
    ] } }
```

`documents` 最多 50 条（`doc_count` 携带真实总数，便于 UI 渲染"还有 N 条"）；当相同字节曾以不同文件名导入时，`original_file_name` 可能与本次上传的名称不同。

!!! note "会话的 410 与 404"
    **已过期**（被 TTL 清理器回收）的会话 id 返回 **410 Gone** —— 提示用户重新上传；**从未存在**
    的 id 返回 **404**。TTL 由 `ingest.session_ttl_minutes` / `session_hard_ttl_minutes` 控制
    （见[配置](configuration.zh.md)）。

### 编辑与提交

- `PUT …/documents/{idx}` —— 部分编辑。只发送你修改的字段（`project`、`equipment`、`title`、
  `error_codes`、各类型专有字段、`notes`、`accepted` 中任意几个）。`idx` 越界返回 `400`。
- `PATCH …/documents/{idx}` —— `{ "accepted": true|false }`。
- `PATCH …/documents/{idx}/resolve` —— 解决冲突（见下文）。
- `POST …/documents/accept-all` —— 可选请求体 `{ "knowledge_type": "alarm" }` 以仅接受某类型；
  返回 `{ "accepted": N }`。
- `POST …/commit` —— 索引每条已接受文档并在追踪索引中记录文件。

### 冲突与交叉引用 {#ingest-conflicts}

分段之后，每条暂存文档都会与线上 KB 比对。若某文档的内容寻址 `doc_id` 已存在，提交时会
**覆盖**那条已提交文档，因此它会被标记（设置 `collision`）并**阻断提交**直到解决 —— 普通提交
绝不会静默覆盖。用以下方式解决：

```json
// PATCH …/documents/{idx}/resolve
{ "action": "merge",
  "merged_fields": { "content": "…合并…", "resolution": "…" } }
```

| `action` | 对提交的影响 |
|---|---|
| `keep` | 跳过该文档 —— 保留现有 KB 文档（计入 `skipped`）。 |
| `overwrite` | 原样索引该文档，替换现有文档。 |
| `merge` | 应用 `merged_fields`（部分编辑 —— 仅内容字段；身份字段被排除，使 `doc_id` 保持稳定），再索引。 |

`GET …/sessions/{id}/summary` 返回 `CommitSummary`，描述提交将做什么 —— 用它驱动审阅横幅并
控制提交按钮：

```json
{ "new": 3, "overwrite": 1, "keep": 1, "unresolved_conflicts": 0,
  "dup_groups": 1, "missing_required": 0, "skipped_duplicate_files": 2, "rejected": 1 }
```

存在未解决冲突的已接受文档（`unresolved_conflicts > 0`）会作为提交错误被报告且**不**索引。

`CommitResponse`：

```json
{
  "committed": 5,
  "skipped": 1,
  "errors": [],
  "vectors_skipped": 0,
  "tracking_failed": 0
}
```

`vectors_skipped` 统计因嵌入服务故障而未带向量索引的文档（仍可 BM25 检索）。`tracking_failed > 0`
表示文档已入 ES 但追踪行未更新 —— 它们会在下次启动重新播种时丢失，故应调用 `recommit-tracking`
恢复：

```json
// POST …/recommit-tracking → RecommitTrackingResponse
{ "recovered": 2, "still_failed": 0, "errors": [] }
```

### 重试

`POST …/files/{file_hash}/retry` 与 `POST …/retry-failed` 对失败文件重跑抽取+切分。请求体可选
`{ "force_ocr": true }`，即便 `ingest.ocr_enabled` 关闭也对本次重试开启 OCR —— 便于在不改服务端
配置的情况下恢复扫描件/纯图片 PDF。二者均返回 `202` 及更新后的 `UploadResponse`；错误的会话/文件
id 返回 `404`。

### 端到端完整示例 {#ingest-walkthrough}

```bash
# 1. 上传 PDF 并附提示
SID=$(curl -s -X POST localhost:8000/api/v1/ingest/upload \
  -F 'files=@alarms.pdf' -F 'knowledge_type_hint=alarm' -F 'project_hint=PDX' \
  | jq -r .session_id)

# 2. 轮询直到 ready_for_review
until [ "$(curl -s localhost:8000/api/v1/ingest/sessions/$SID | jq -r .status)" \
        = "ready_for_review" ]; do sleep 1; done

# 3.（可选）修正 0 号文档被误抽的字段
curl -s -X PUT localhost:8000/api/v1/ingest/sessions/$SID/documents/0 \
  -H 'content-type: application/json' -d '{"equipment":"Aligner"}'

# 4. 全部接受并提交
curl -s -X POST localhost:8000/api/v1/ingest/sessions/$SID/documents/accept-all
curl -s -X POST localhost:8000/api/v1/ingest/sessions/$SID/commit | jq

# 5. 已提交文档即可检索
curl -s localhost:8000/api/v1/search -H 'content-type: application/json' \
  -d '{"knowledge_type":"alarm","project":"PDX","keywords":["真空"],"mode":"auto"}' | jq
```

---

## Taxonomy 与管理 {#taxonomy}

| 方法与路径 | 用途 | 成功 |
|---|---|---|
| `GET /api/v1/facets` | 返回实时 taxonomy（`Taxonomy` 模型） | `200` |
| `POST /api/v1/admin/reload-taxonomy` | 无需重启重载 `config/taxonomy.yaml` | `200` |
| `GET /api/v1/admin/search-feedback?limit=20` | 聚合 👍/👎 反馈用于排序调优 | `200` / `503` |

`GET /api/v1/facets` →

```json
{
  "version": "2026-05-19-r1",
  "knowledge_types": ["alarm", "setup", "experience"],
  "projects": ["Kinneret", "MEM", "MHK", "PDX", "Boston", "Sonora", "Yucatan", "所有项目"],
  "equipment": ["Aligner", "Conveyor", "FTU", "Heater", "Loader", "Pump", "SensorModule", "Stage"]
}
```

taxonomy 同时支撑索引时的过滤校验与 LLM 抽取引导 —— 见
[数据模型 → Taxonomy](reference/data-model.zh.md#taxonomy-model)。

---

## 反馈 {#feedback}

| 方法与路径 | 用途 | 成功 |
|---|---|---|
| `POST /api/v1/search/feedback` | 记录一次结果 👍/👎 | `202` `{status:"recorded"}` |

仅作观测 —— 绝不改变结果。请求体（`FeedbackRequest`）：

```json
{
  "doc_id": "kb_alarm_v1:9f2c…",
  "helpful": false,
  "query_text": "E-1234 aligner fault",
  "knowledge_type": "alarm",
  "project": "PDX",
  "equipment": "Aligner",
  "search_status": "loose_hit"
}
```

记录尽力而为：存储故障返回 **503** 而非中断用户流程。存储 schema 与管理聚合见
[可观测性 → 搜索反馈](observability.zh.md#search-feedback)。

---

## 运维与元接口 {#operational}

| 方法与路径 | 用途 |
|---|---|
| `GET /healthz` | 存活性 —— 进程在线；不探测依赖。恒返回 `200 {status:"ok"}` |
| `GET /readyz` | 就绪性 —— ping ES；ES 可达返回 `200`，否则 `503`（降级） |
| `GET /readyz?deep=true` | 额外往返嵌入服务；报告 `embedding: ok\|down\|disabled\|configured` |
| `GET /metrics` | Prometheus 指标（当 `observability.metrics_enabled`） |
| `GET /docs`、`/redoc`、`/openapi.json` | 交互式 API 文档 |
| `GET /` | 内置单页前端（`Knowledge Base Search.html`） |

`GET /readyz?deep=true` 响应体：

```json
{ "status": "ok", "es": "ok", "embedding": "ok", "llm": "configured" }
```

如何将这些接口接入编排器，见[可观测性 → 健康检查接口](observability.zh.md#health)。
