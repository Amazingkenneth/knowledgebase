# 故障排查 {#troubleshooting}

按子系统分组的 现象 → 原因 → 处理 矩阵，覆盖你最可能遇到的故障。本页引用的指标与健康探针见
[可观测性](../observability.zh.md)。

---

## 启动与 Elasticsearch {#startup}

| 现象 | 原因 | 处理 |
|---|---|---|
| `GET /readyz` 返回 **503**，日志显示 *"Elasticsearch unreachable … DEGRADED mode"* | 应用早于 ES 就绪而启动，或 `KB_ES__URL` 错误 | 等待 ES 健康（compose 的 `depends_on: service_healthy` 已处理）；核对 `KB_ES__URL`。应用以降级启动并在 ES 起来后恢复 —— 在此之前搜索/索引失败 |
| 索引失败，报**分词器未找到**/映射错误 | ES 无 `analysis-ik` 插件，但映射要 `ik_max_word`/`ik_smart` | 使用自带的 `elasticsearch/Dockerfile`（安装 IK），**或**将 `KB_ES__ANALYZER_INDEX` 与 `KB_ES__ANALYZER_QUERY` 都设为 `cjk`（内置二元分词） |
| 与 ES 的 TLS 握手/证书错误 | HTTPS ES 用自签证书 | 设 `KB_ES__SSL_FINGERPRINT`（ES 启动输出的 SHA-256），或仅本地开发用 `KB_ES__VERIFY_CERTS=false` |

---

## 搜索与排序 {#search}

| 现象 | 原因 | 处理 |
|---|---|---|
| 有结果但明显**仅关键词**（无语义匹配）；`kb_upstream_errors_total{service="embedding"}` 上升 | 嵌入服务故障/未配置 —— 搜索已静默降级为 BM25 | 设置/修复 `KB_EMBEDDING__API_KEY` + `KB_EMBEDDING__URL`；确认 `GET /readyz?deep=true` 报 `embedding: ok` |
| 启用嵌入后每次向量搜索都报错 | `KB_EMBEDDING__DIMS` ≠ 模型真实维度；ES 拒绝 `dense_vector` | 将 `dims` 设为模型输出（`text-embedding-v3` 默认 `1024`）；维度变更需**重新播种**（索引会被重建） |
| 状态总是 `too_many` | 严格匹配超过 `strict_max_hits`（默认 8） | 符合预期 —— 缩小查询，或调大 `KB_SEARCH__STRICT_MAX_HITS` |
| `POST /api/v1/search` 返回 **422** | `from_ + size > 10000`，或某字段越界 | 减少翻页深度（上限对齐 ES `index.max_result_window`）；用缩小代替深翻 |
| 按某 project/equipment 过滤无结果 | 该值不在 taxonomy 中（故 LLM 给出的值被丢弃，或你按未知字面量过滤） | 查看 `GET /api/v1/facets`；将值加入 `config/taxonomy.yaml` 并重载 |

---

## AI 对话与抽取 {#chat}

| 现象 | 原因 | 处理 |
|---|---|---|
| `/chat` 或 `/extract` 返回 **503** "LLM not configured" | 未设 `KB_LLM__API_KEY` | 设置密钥；按部署方式重启或在调用时读取 |
| `/extract` 返回 **502** "unparseable response" | LLM 返回非 JSON/格式错误 | 通常瞬时 —— 已自动重试（`KB_LLM__MAX_RETRIES`）；若持续，确认模型能输出近 JSON，且 `KB_LLM__MODEL` 有效 |
| `/chat` 回答称检索不可用（`search_error: true`） | KB 检索抛错（如 ES 故障）—— **并非**真正的 no-hit | 修复 ES；模型被刻意告知检索宕机，而非凭空作答 |
| 抽取的 `equipment`/`project` 总返回 `null` | 抽取器只填用户明确点名的 taxonomy 值；"宁填 null 不猜" | 这是对错误过滤的刻意防护 —— 说出确切的 taxonomy 名，或直接向 `/search` 传过滤值 |

---

## 文件导入 {#import}

| 现象 | 原因 | 处理 |
|---|---|---|
| 某 PDF 文件以 扫描件/纯图片 错误结束（`ScannedPdfError`） | PDF 无文本层且 OCR 关闭 | 启用 OCR（`KB_INGEST__OCR_ENABLED=true` + `ocr` extra / `INSTALL_OCR=true` 镜像），或用 `force_ocr` 重试该文件：`POST …/files/{hash}/retry {"force_ocr":true}` |
| 文件处理完成但界面显示 **"未提取到文档 / No documents extracted"**（0 条暂存文档） | 文本抽取正常，但切分未返回任何条目——通常是超预算的块被 LLM 截断，或全部条目落入被跳过的块 | 已在 `chunk_pages`（重叠现为预算受限，每块 ≤ `segmentation_chunk_chars`）与 `_split_oversized_page`（保留首个标题前的前导文本）中修复。查看该文件的 `skipped_chunks` 是否为 `no_entries`/`parse_failed`；调小 `KB_INGEST__SEGMENTATION_CHUNK_CHARS` 或设定知识类型提示后重新上传 |
| 文件 `status: skipped_duplicate` | 其 SHA-256 哈希已提交过 | 刻意的去重；若确实要重导，上传/扫描时带 `force=true` |
| 某暂存文档显示 **冲突** 徽章且提交被禁用 / 提交错误提示 *"Unresolved conflict"* | 该文档的身份（`doc_id` = 类型+项目+机台+标题+报警代码）在知识库中已存在，提交会覆盖它 | 打开 **比较并解决**，选择保留 / 覆盖 / 合并（`PATCH …/documents/{idx}/resolve`）。如需恢复旧的静默覆盖行为，设 `KB_INGEST__COLLISION_DETECTION_ENABLED=false` |
| 文件 `status: unsupported` | 扩展名不在 `allowed_extensions` 中 | 加入 `KB_INGEST__ALLOWED_EXTENSIONS`（默认 pdf/xlsx/xls/csv/pptx/docx） |
| 大文件被拒 | 超 `max_file_size_mb`（50），或 PDF 超 `pdf_max_pages`（2000）/ XLSX 超 `xlsx_max_cells`（200 万） | 调大上限，或拆分文件 —— 这些是抽取期防 OOM 的护栏 |
| `GET /sessions/{id}` 返回 **410** | 会话已过期并被 TTL 清理器回收 | 重新上传；调 `KB_INGEST__SESSION_TTL_MINUTES` / `SESSION_HARD_TTL_MINUTES` |
| `GET /sessions/{id}` 返回 **404** | 该 id 从未存在（区别于 410 = 过期） | 核对 id；它来自最初的 upload/scan 响应 |
| 提交返回 `tracking_failed > 0` | 文档已入 ES 但追踪行未更新 | 调用 `POST …/recommit-tracking` —— 否则下次启动重播种时会丢失 |

---

## 持久化与数据 {#persistence}

| 现象 | 原因 | 处理 |
|---|---|---|
| 改了 CSV 但变更未出现 | 播种仅在启动时发生 | 重启应用 —— `seed()` 每次启动清空每个主索引并从 CSV 重载 |
| 重启后导入文档消失 | 它们不在 CSV 中；恢复读取追踪索引 | 确认 `kb_import_files` 完好；`restore_imports()` 在启动时重放已提交导入。若提交时发生 `tracking_failed`，它们从未被追踪 —— 需重导 |
| 改嵌入模型后文档消失 | `dims` 变更重建索引；随后的强制重播种从 CSV/追踪索引重载 | 符合预期；确保追踪索引存活以便导入恢复 |

---

## 文档站点 {#docs}

| 现象 | 原因 | 处理 |
|---|---|---|
| `mkdocs build --strict` 因链接/锚点损坏失败 | 跨页链接或 `{#anchor}` 无法解析 | 修复链接；strict 模式还会捕获 `navigation.instant` ↔ i18n 不兼容 |
| `git push` 被 `pre-push` 钩子中止 | strict 文档构建失败 | 修复文档，或用 `git push --no-verify` 一次性绕过（见[部署 → 文档自动发布](deployment.zh.md#docs-publish)） |
| 切换 zh⇄en 时语言切换器 404 | GitHub Pages 子路径下 `site_url` 缺失/错误 | 确保 `mkdocs.yml` 设了 `site_url`（切换器需要它来生成带基础前缀的链接） |

---

当处理方式不明显时，用请求的 `request_id`（每条日志都带）在日志中检索，即可重放该次调用的全过程 ——
见[可观测性 → 日志](../observability.zh.md#logging)。
