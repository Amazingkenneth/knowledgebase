# 配置项完整参考 {#config-reference}

本页穷举 `src/kb/config.py` 中 `Settings` 的**每一个**配置项：YAML 路径、对应环境变量、
类型、默认值、取值范围与作用。照此即可完整复现运行时配置面。概念性介绍见
[配置](../configuration.md)。

---

## 加载与优先级 {#precedence}

配置分层合并，`settings_customise_sources()` 定义优先级（高 → 低）：

1. 初始化关键字参数（`Settings(...)`，主要用于测试）
2. **shell 环境变量**
3. **`.env` 文件**（`pydantic-settings` 自动加载，已 git-ignore）
4. **`config/settings.yaml`**
5. file secrets

!!! note "为什么 YAML 排在环境变量之下"
    这样 `KB_*` 覆盖（例如 docker-compose 注入的 `KB_ES__URL=http://elasticsearch:9200`）
    才能压过 `settings.yaml` 里的默认值。

环境变量命名规则：

- 前缀：`KB_`
- 嵌套分隔符：`__`（双下划线）
- 例：`settings.llm.api_key` ↔ `KB_LLM__API_KEY`；`settings.search.title_boost` ↔
  `KB_SEARCH__TITLE_BOOST`
- 列表类型用 JSON：`KB_INGEST__ALLOWED_EXTENSIONS='["pdf","csv"]'`

`model_config`：`env_file=".env"`、`env_file_encoding="utf-8"`、`env_prefix="KB_"`、
`env_nested_delimiter="__"`、`extra="ignore"`（未知键忽略）、
`yaml_file="config/settings.yaml"`。

`get_settings()` 带 `@lru_cache(maxsize=1)`——进程内只加载一次，运行时改 `.env` 或
`settings.yaml` 需重启才生效（分类法 `taxonomy.yaml` 例外，可通过 admin 端点热重载）。

---

## `es` — Elasticsearch（`ESConfig`） {#es}

| 字段 | 环境变量 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `url` | `KB_ES__URL` | str | `https://localhost:9200` | ES 地址。docker-compose 注入 `http://elasticsearch:9200` |
| `index_prefix` | `KB_ES__INDEX_PREFIX` | str | `kb` | 索引/别名前缀（`kb_alarm` 等） |
| `request_timeout_s` | `KB_ES__REQUEST_TIMEOUT_S` | int | `10` | 单次请求超时（秒） |
| `ssl_fingerprint` | `KB_ES__SSL_FINGERPRINT` | str\|None | `None` | 服务端 TLS 证书 SHA-256 指纹；设了就免 CA、可用自签证书 |
| `verify_certs` | `KB_ES__VERIFY_CERTS` | bool | `True` | 是否校验证书；本地开发可设 `false` |
| `username` | `KB_ES__USERNAME` | str\|None | `None` | basic auth 用户名 |
| `password` | `KB_ES__PASSWORD` | str\|None | `None` | basic auth 密码（建议用环境变量） |
| `analyzer_index` | `KB_ES__ANALYZER_INDEX` | str | `ik_max_word` | 索引分析器；无 IK 插件时设 `cjk` |
| `analyzer_query` | `KB_ES__ANALYZER_QUERY` | str | `ik_smart` | 查询分析器；无 IK 插件时设 `cjk` |

客户端构造逻辑（`src/kb/es/client.py`）：仅当 `username` 与 `password` 同时存在才启用
basic auth；设了 `ssl_fingerprint` 用指纹固定（免 CA），否则若 `verify_certs=false`
则关闭证书校验。

!!! warning "settings.yaml 里的默认是 HTTP"
    `config/settings.yaml` 把 `url` 设为 `http://localhost:9200`、`verify_certs: false`，
    以匹配 docker-compose 中 `xpack.security.enabled=false` 的明文 ES。生产请改回
    HTTPS 并配置指纹/账号密码。

---

## `embedding` — 向量嵌入（`EmbeddingConfig`） {#embedding}

| 字段 | 环境变量 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|---|
| `url` | `KB_EMBEDDING__URL` | str | `http://localhost:8080` | — | OpenAI 兼容 embeddings 端点的 base（实际请求 `POST {url}/embeddings`） |
| `api_key` | `KB_EMBEDDING__API_KEY` | str | `""` | — | **为空则禁用向量检索**（退化为 BM25-only） |
| `model` | `KB_EMBEDDING__MODEL` | str | `text-embedding-v3` | — | 模型名 |
| `dims` | `KB_EMBEDDING__DIMS` | int | `1024` | — | 输出维度；**必须等于模型实际维度**，否则写入报错 |
| `batch_size` | `KB_EMBEDDING__BATCH_SIZE` | int | `10` | 1–128 | 每批条数；DashScope 兼容端点上限为 10 |
| `timeout_s` | `KB_EMBEDDING__TIMEOUT_S` | int | `30` | — | 请求超时（秒） |

`settings.yaml` 默认把 `url` 设为 `https://dashscope.aliyuncs.com/compatible-mode/v1`。
客户端会规范化为带末尾 `/` 的 base，再以相对路径 `embeddings` 发请求。响应按 `index`
排序保证输入顺序，并逐条校验维度。内置 3 次指数退避重试（`tenacity`）。

---

## `search` — 检索调优（`SearchConfig`） {#search}

| 字段 | 环境变量 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|---|
| `strict_max_hits` | `KB_SEARCH__STRICT_MAX_HITS` | int | `8` | 1–50 | strict 命中超过此数 → `too_many` |
| `title_boost` | `KB_SEARCH__TITLE_BOOST` | float | `3.0` | 1.0–10.0 | BM25 中 `title` 相对 `body` 的权重 |
| `rrf_window` | `KB_SEARCH__RRF_WINDOW` | int | `50` | 10–500 | 参与 BM25+向量重排的 top 召回候选数（rescore 窗口） |
| `vector_weight` | `KB_SEARCH__VECTOR_WEIGHT` | float | `0.5` | 0.0–1.0 | 最终分中向量分的权重 |
| `max_result_window` | `KB_SEARCH__MAX_RESULT_WINDOW` | int | `10000` | 10–100000 | `from_+size` 上限，镜像 ES `index.max_result_window` |

最终打分公式（嵌入可用时，对 rescore 窗口内候选）：

```
final_score = (1 - vector_weight) × BM25 + vector_weight × (cosine_sim + 1)
```

`cosine_sim + 1` 把 `[-1,1]` 映射到 `[0,2]` 以保证非负。调参方法见
[检索与排序](../architecture/search-ranking.md) 与
[可观测性 § 检索反馈](../observability.md#search-feedback)。

!!! note "`max_result_window` 的双重存在"
    模型层 `SearchRequest` 用一个**字面量** `10000` 校验 `from_+size`（保持模型不耦合
    settings）；此处 `max_result_window` 是同一上限的可配置版本，作运维记录之用。

---

## `taxonomy` — 分类法（`TaxonomyConfig`） {#taxonomy}

| 字段 | 环境变量 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `path` | `KB_TAXONOMY__PATH` | str | `config/taxonomy.yaml` | 分类法 YAML 路径 |

该文件在启动时会被 `_sync_taxonomy_from_es()` 改写（追加 ES 中发现的新值、改写
`version`），**运行时必须可写**。可通过 `POST /api/v1/admin/reload-taxonomy` 热重载。

---

## `llm` — 大模型（`LLMConfig`） {#llm}

| 字段 | 环境变量 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|---|
| `api_url` | `KB_LLM__API_URL` | str | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` | — | OpenAI 兼容 chat completions 端点 |
| `api_key` | `KB_LLM__API_KEY` | str | `""` | — | **为空则 `/chat`、`/extract`、导入分段返回 503** |
| `model` | `KB_LLM__MODEL` | str | `qwen-plus` | — | 模型名 |
| `max_tokens` | `KB_LLM__MAX_TOKENS` | int | `1200` | — | 默认最大输出 tokens（分段调用会按需覆盖） |
| `timeout_s` | `KB_LLM__TIMEOUT_S` | int | `20` | 1–300 | 默认聊天读超时（秒） |
| `extract_timeout_s` | `KB_LLM__EXTRACT_TIMEOUT_S` | int | `10` | 1–300 | `/extract` 结构化抽取的较短预算 |
| `max_retries` | `KB_LLM__MAX_RETRIES` | int | `2` | 0–5 | 瞬时失败（429/5xx/超时）重试次数，0 关闭 |

所有 provider 都必须实现 OpenAI Chat Completions 协议；换 provider 只需改这三项即可，
无需改代码：

```bash
KB_LLM__API_KEY=your-key
KB_LLM__API_URL=https://api.openai.com/v1/chat/completions
KB_LLM__MODEL=gpt-4o-mini
```

客户端（`src/kb/services/llm.py`）：429 与 5xx 视为可重试，其他 4xx 永久失败；分段调用
（`src/kb/services/segmentation.py`）会根据消息体大小推导一个更长的读超时，覆盖
`timeout_s`。

---

## `server` — 服务器（`ServerConfig`） {#server}

| 字段 | 环境变量 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|---|
| `host` | `KB_SERVER__HOST` | str | `0.0.0.0` | — | 监听地址 |
| `port` | `KB_SERVER__PORT` | int | `8000` | 1–65535 | 监听端口 |

!!! note "端口读取的差异"
    `python -m kb`（`src/kb/__main__.py`）会读取 `server.port`/`server.host` 作为
    `argparse` 默认；而 `uvicorn kb.main:app` 直接启动**不读** settings，需用 `--port`。

---

## `ingest` — 文件导入（`IngestConfig`） {#ingest}

| 字段 | 环境变量 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|---|
| `upload_dir` | `KB_INGEST__UPLOAD_DIR` | str | `data/uploads` | — | 上传文件落盘目录 |
| `scan_root` | `KB_INGEST__SCAN_ROOT` | str | `data` | — | 服务端文件夹扫描的根，扫描限制在此目录内（防路径穿越） |
| `max_file_size_mb` | `KB_INGEST__MAX_FILE_SIZE_MB` | int | `50` | 1–500 | 单文件大小上限（MB） |
| `allowed_extensions` | `KB_INGEST__ALLOWED_EXTENSIONS` | list[str] | `["pdf","xlsx","xls","csv","pptx","docx"]` | — | 允许的扩展名 |
| `pdf_max_pages` | `KB_INGEST__PDF_MAX_PAGES` | int | `2000` | 1–50000 | PDF 页数上限（防 OOM） |
| `xlsx_max_cells` | `KB_INGEST__XLSX_MAX_CELLS` | int | `2000000` | ≥1000 | 表格单元格上限（防 OOM） |
| `ocr_enabled` | `KB_INGEST__OCR_ENABLED` | bool | `True` | — | 是否启用 PaddleOCR 回退 |
| `ocr_lang` | `KB_INGEST__OCR_LANG` | str | `ch` | — | OCR 语言模型；`ch` 也识别拉丁字符，`en` 为纯英文 |
| `ocr_min_confidence` | `KB_INGEST__OCR_MIN_CONFIDENCE` | float | `0.5` | 0.0–1.0 | 丢弃低于此置信度的 OCR 行 |
| `segmentation_max_tokens` | `KB_INGEST__SEGMENTATION_MAX_TOKENS` | int | `8000` | — | 分段 LLM 最大输出 tokens |
| `segmentation_chunk_chars` | `KB_INGEST__SEGMENTATION_CHUNK_CHARS` | int | `12000` | 1000–100000 | 每个 LLM 分段块的字符数 |
| `session_ttl_minutes` | `KB_INGEST__SESSION_TTL_MINUTES` | int | `120` | 10–1440 | 软 TTL：清理已 committed/failed 的会话 |
| `session_hard_ttl_minutes` | `KB_INGEST__SESSION_HARD_TTL_MINUTES` | int | `480` | 10–10080 | 硬 TTL：清理任意会话（含审阅中），防内存泄漏 |
| `session_evict_interval_minutes` | `KB_INGEST__SESSION_EVICT_INTERVAL_MINUTES` | int | `15` | 1–1440 | 后台清理器扫描间隔 |

!!! warning "OCR 依赖需单独安装"
    `ocr_enabled=true` 仅是开关；运行时还需安装 PaddleOCR（`ocr` extra，或镜像构建参数
    `INSTALL_OCR=true`）。未安装时遇到扫描件 PDF 会给出可操作的提示而非崩溃。

`segmentation_chunk_chars` 取舍：越大 → API 调用越少但每次 token 越多。12000 字符约
3000–4000 输入 tokens，能容纳 6–10 条报警条目。

---

## `observability` — 可观测性（`ObservabilityConfig`） {#observability}

| 字段 | 环境变量 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `metrics_enabled` | `KB_OBSERVABILITY__METRICS_ENABLED` | bool | `True` | 是否暴露 `GET /metrics`（Prometheus 文本） |
| `json_logs` | `KB_OBSERVABILITY__JSON_LOGS` | bool | `False` | `true` → 每行一个 JSON 日志对象 |
| `log_level` | `KB_OBSERVABILITY__LOG_LEVEL` | str | `INFO` | 日志级别 |

详见 [可观测性](../observability.md)。

---

## 关键降级开关速查 {#degradation}

| 缺失项 | 影响 |
|---|---|
| `KB_LLM__API_KEY` | `/chat`、`/extract` 返回 503；导入分段不可用；检索/索引照常 |
| `KB_EMBEDDING__API_KEY`（或服务不可达） | 无向量重排、无 kNN 回退 → 仅 BM25；服务正常启动 |
| ES 不可达 | 启动进入 DEGRADED 模式；检索/索引失败直到恢复（`/readyz` 返回 503） |
| 无 IK 插件 | 把 `KB_ES__ANALYZER_INDEX`/`KB_ES__ANALYZER_QUERY` 设为 `cjk` |
| 无 OCR 依赖 | 扫描件 PDF 报可操作错误，其余文件正常 |

完整的环境变量模板见仓库根的 `.env.example`（已带每项默认值注释）。
