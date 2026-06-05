# API 参考

所有接口在 `http://<host>:8000` 下提供。交互式、始终最新的 schema 由 FastAPI 生成：

- **Swagger UI** —— `/docs`
- **ReDoc** —— `/redoc`
- **OpenAPI JSON** —— `/openapi.json`

本页是人工整理的接口地图。精确的请求/响应模型请优先看 `/docs`（它永远不会与代码漂移）。

---

## 搜索

### `POST /api/v1/search`

结构化混合检索。请求体为 `SearchRequest`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `knowledge_type` | `alarm\|setup\|experience\|null` | `null` 检索所有类型 |
| `project` | `str\|null` | 精确匹配过滤（taxonomy） |
| `equipment` | `str\|null` | 精确匹配过滤（taxonomy） |
| `error_codes` | `list[str]` | 文档包含其中任一即匹配 |
| `keywords` | `list[str]` | BM25 关键词 |
| `query_text` | `str\|null` | 用于向量阶段的原始文本 |
| `mode` | `auto\|strict\|loose\|vector_only` | 默认 `auto` |
| `size`、`from_` | `int` | 分页；受 `max_result_window` 约束 |

返回 `SearchResponse`，含 `status`、`total`、`hits[]`、`effective_params`，可选的 `facets` + `facets_truncated`（在 `too_many` 时），以及 `banner`（在 `loose_hit` / `vector_only` 时）。见[状态契约](architecture/search-ranking.md#status-contract)。

---

## 对话与提取

二者均需 `KB_LLM__API_KEY`（否则 **503**）。见 [AI 对话搜索](architecture/ai-chat.md)。

### `POST /api/v1/chat`

对话搜索。请求体：`{ messages: [{role, content}], last_search_params? }`。返回 `{ content, search_results?, search_status?, effective_params?, search_error }`。

### `POST /api/v1/extract`

NL → 结构化参数。请求体：`{ query }`。返回 `{ project, knowledge_type, error_codes, equipment, keywords, is_sentence }`。

---

## 文档 {#documents}

直接对类型索引 CRUD（`src/kb/api/documents.py`）。

| 方法与路径 | 用途 |
|---|---|
| `GET /api/v1/documents/stats` | 按类型 / 项目 / 机台 / 报警码聚合的文档计数（驱动落地页） |
| `POST /api/v1/documents/{knowledge_type}` | 索引单个文档 → `201 { id }` |
| `POST /api/v1/documents/{knowledge_type}/_bulk` | 批量索引；运行前先暴露所有解析错误 |
| `DELETE /api/v1/documents/{knowledge_type}/{doc_id}` | 按 id 删除 → `204`，不存在则 `404` |

`{knowledge_type}` 对照枚举校验；请求体被判别到对应的 `AlarmDoc` / `SetupDoc` / `ExperienceDoc` 模型并对照 taxonomy 校验。

---

## Ingest（文件导入）

强制人工审核的文件 → 文档管道（`src/kb/api/ingest.py`）。需要 `KB_LLM__API_KEY`。完整设计见[文件导入管道](architecture/import-pipeline.md)。

| 方法与路径 | 用途 |
|---|---|
| `POST /api/v1/ingest/upload` | multipart 上传 → `202 { session_id }` |
| `POST /api/v1/ingest/scan` | 扫描服务端文件夹（在 `scan_root` 内）→ `202` |
| `GET /api/v1/ingest/sessions` | 列出会话 |
| `GET /api/v1/ingest/sessions/{id}` | 查询会话（轮询至 `ready_for_review`） |
| `PUT /api/v1/ingest/sessions/{id}/documents/{idx}` | 编辑暂存文档 |
| `PATCH /api/v1/ingest/sessions/{id}/documents/{idx}` | 接受 / 拒绝暂存文档 |
| `POST /api/v1/ingest/sessions/{id}/documents/accept-all` | 接受全部暂存文档 |
| `POST /api/v1/ingest/sessions/{id}/commit` | 将已接受文档写入 ES |

重试接口可重新处理失败文件（可选强制 OCR）。

---

## Taxonomy 与管理

| 方法与路径 | 用途 |
|---|---|
| `GET /api/v1/facets` | 返回实时 taxonomy（`Taxonomy` 模型） |
| `POST /api/v1/admin/reload-taxonomy` | 无需重启重载 `taxonomy.yaml` |
| `GET /api/v1/admin/search-feedback` | 聚合 👍/👎 反馈用于排序调优 |

---

## 反馈

| 方法与路径 | 用途 |
|---|---|
| `POST /api/v1/search/feedback` | 记录一次结果 👍/👎 → `202`。仅观察用——绝不改变结果 |

请求体：`{ doc_id, helpful, query_text?, knowledge_type?, project?, equipment?, search_status? }`。见[可观测性 → 搜索反馈](observability.md#search-feedback)。

---

## 运维

| 方法与路径 | 用途 |
|---|---|
| `GET /metrics` | Prometheus 指标（当 `observability.metrics_enabled`） |
| `GET /docs`、`/redoc`、`/openapi.json` | 交互式 API 文档 |
