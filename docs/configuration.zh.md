# 配置

设置分层，优先级从高到低：

```
shell 环境变量  →  .env  →  config/settings.yaml  →  内置默认值
```

Schema 是 `src/kb/config.py` 中的 pydantic-settings `Settings` 类——所有字段的唯一事实来源。`config/settings.yaml` 保存运行时默认值；`.env`（git 忽略）保存机密，由 pydantic-settings 自动加载。将 `.env.example` 复制为 `.env` 并填入取值。

!!! info "环境变量命名"
    所有环境变量使用前缀 `KB_`，嵌套分隔符为 `__`。`KB_LLM__API_KEY` → `settings.llm.api_key`，`KB_ES__URL` → `settings.es.url`。`settings.yaml` 的优先级**低于**环境变量，因此 `KB_*` 覆盖（如 docker-compose 注入的 `KB_ES__URL`）会胜过文件中的默认值。

---

## 关键环境变量

| 环境变量 | 未设置时的影响 |
|---|---|
| `KB_LLM__API_KEY` | `/api/v1/chat`、`/extract` 及所有 `/ingest/*` 接口返回 **503**。搜索与索引照常工作。 |
| `KB_EMBEDDING__API_KEY` | 禁用向量重排与 kNN 降级——**仅 BM25** 关键词检索。服务正常启动。 |
| `KB_ES__ANALYZER_INDEX` / `KB_ES__ANALYZER_QUERY` | 默认 `ik_max_word` / `ik_smart`。若 ES 未装 IK 插件，将二者均设为 `cjk`。 |

---

## 配置分组

### Elasticsearch（`es`）

| 键 | 默认值 | 说明 |
|---|---|---|
| `url` | `https://localhost:9200` | docker-compose 覆盖为 `http://elasticsearch:9200` |
| `index_prefix` | `kb` | 所有索引/别名的前缀 |
| `request_timeout_s` | `10` | 单次 ES 请求超时 |
| `ssl_fingerprint` | `null` | 自签名 HTTPS 的 SHA-256 证书指纹 |
| `verify_certs` | `true` | 仅本地开发可设为 `false` |
| `username` / `password` | `null` | 安全集群的基本认证 |
| `analyzer_index` | `ik_max_word` | 入库分词器（IK 插件）；兜底 `cjk` |
| `analyzer_query` | `ik_smart` | 查询分词器；兜底 `cjk` |

### Embedding（`embedding`）

| 键 | 默认值 | 说明 |
|---|---|---|
| `url` | `http://localhost:8080` | 生产环境用 DashScope OpenAI 兼容端点 |
| `api_key` | `""` | 启用向量检索所必需 |
| `model` | `text-embedding-v3` | 默认 1024 维 |
| `dims` | `1024` | 必须与 `body_vec` 映射一致 |
| `batch_size` | `10` | DashScope 拒绝 > 10 的批次 |
| `timeout_s` | `30` | 单次请求超时 |

### Search（`search`） {#search}

| 键 | 默认值 | 作用 |
|---|---|---|
| `strict_max_hits` | `8` | `too_many` 阈值 |
| `title_boost` | `3.0` | BM25 中标题相对正文的权重 |
| `rrf_window` | `50` | 参与向量重排的召回命中数 |
| `vector_weight` | `0.5` | 最终评分中 BM25 ↔ 余弦的平衡 |
| `max_result_window` | `10000` | 最深 `from_ + size`；对应 ES `index.max_result_window` |

各旋钮的作用见[检索与排序](architecture/search-ranking.md)。

### LLM（`llm`）

| 键 | 默认值 | 说明 |
|---|---|---|
| `api_url` | DashScope 兼容模式 URL | 任意 OpenAI Chat Completions 端点 |
| `api_key` | `""` | 来自 `.env`；启用 AI 接口 |
| `model` | `qwen-plus` | Provider 的模型 id |
| `max_tokens` | `1200` | 单次补全最大 token 数 |
| `timeout_s` | `20` | 默认对话读超时 |
| `extract_timeout_s` | `10` | `/extract` 的更短预算 |
| `max_retries` | `2` | 瞬时失败重试（429 / 5xx / 超时） |

### Ingest（`ingest`） {#ingest}

| 键 | 默认值 | 作用 |
|---|---|---|
| `upload_dir` | `data/uploads` | 上传文件落盘目录（`<hash>_<name>`） |
| `scan_root` | `data` | `POST /ingest/scan` 限制在此根目录内 |
| `max_file_size_mb` | `50` | 单文件上限；超限 → FAILED |
| `allowed_extensions` | `pdf, xlsx, xls, csv, pptx, docx` | 其它扩展名 → UNSUPPORTED |
| `pdf_max_pages` | `2000` | 抽取时的内存守护 |
| `xlsx_max_cells` | `2_000_000` | 抽取时的内存守护 |
| `ocr_enabled` | `true` | 关闭后图片型 PDF 页返回空 |
| `ocr_lang` | `ch` | PaddleOCR 语言包（`ch` 也能识别拉丁字符） |
| `ocr_min_confidence` | `0.5` | 丢弃低于此置信度的 OCR 行 |
| `segmentation_max_tokens` | `8000` | 切分调用的最大 token 数 |
| `segmentation_chunk_chars` | `12000` | 每个 LLM 块的字符数 |
| `session_ttl_minutes` | `120` | 软 TTL：回收 COMMITTED/FAILED 会话 |
| `session_hard_ttl_minutes` | `480` | 硬 TTL：回收任意会话，约束内存 |
| `session_evict_interval_minutes` | `15` | 后台清扫器周期 |

### Observability（`observability`） {#observability}

| 键 | 默认值 | 说明 |
|---|---|---|
| `metrics_enabled` | `true` | 在 `GET /metrics` 暴露 Prometheus 指标 |
| `json_logs` | `false` | 为 true 时每行一个 JSON 对象 |
| `log_level` | `INFO` | 根日志级别 |

### Server（`server`）

| 键 | 默认值 | 说明 |
|---|---|---|
| `host` | `0.0.0.0` | 绑定地址（`python -m kb` 使用） |
| `port` | `8000` | 绑定端口；也可用 `KB_SERVER__PORT` |

---

## Taxonomy

`config/taxonomy.yaml` 是可过滤枚举的**唯一事实来源**——`knowledge_types`、`projects`、`equipment`。它在两处被消费：

1. **LLM 提示注入** —— 提取/切分提示词列出合法值，使模型保持在词汇表内。
2. **入库校验** —— `project` 与 `equipment` 据此校验；未知值被拒绝。

```yaml
version: "2026-05-19-r1"
knowledge_types: [alarm, setup, experience]
projects: [Kinneret, MEM, MHK, PDX, Boston, Sonora, Yucatan, 所有项目]
equipment: [Aligner, Conveyor, FTU, Heater, Loader, ...]
```

要新增项目或设备，编辑此文件。可**无需重启**重载：

```bash
curl -X POST http://localhost:8000/api/v1/admin/reload-taxonomy
```

`GET /api/v1/facets` 返回实时 taxonomy。每次改动文件时请提升 `version`，以便分面消费方能检测到变化。

!!! warning "Taxonomy 在启动时被重写"
    启动时的 taxonomy 自动同步会重写 `config/taxonomy.yaml`，因此该文件必须保持可写（docker-compose 绑定挂载已保证）。引用了被移除值的现有文档，会在下次重 seed 时校验失败。

---

## 接入新的 LLM Provider

仅需覆盖两个环境变量——无需改代码（所有 Provider 都须实现 OpenAI Chat Completions API）：

```bash
KB_LLM__API_KEY=your-key
KB_LLM__API_URL=https://api.openai.com/v1/chat/completions
KB_LLM__MODEL=gpt-4o-mini
```
