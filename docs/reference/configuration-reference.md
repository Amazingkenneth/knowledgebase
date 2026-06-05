# Full Configuration Reference {#config-reference}

This page exhaustively documents **every** setting in the `Settings` schema
(`src/kb/config.py`): YAML path, environment variable, type, default, bounds, and
effect. Implement to this and you reproduce the runtime configuration surface exactly.
For the conceptual overview see [Configuration](../configuration.md).

---

## Loading & precedence {#precedence}

Settings are layered. `settings_customise_sources()` defines precedence (highest →
lowest):

1. init kwargs (`Settings(...)`, mainly for tests)
2. **shell environment variables**
3. **`.env` file** (auto-loaded by `pydantic-settings`, git-ignored)
4. **`config/settings.yaml`**
5. file secrets

!!! note "Why YAML ranks below env vars"
    So `KB_*` overrides (e.g. docker-compose's
    `KB_ES__URL=http://elasticsearch:9200`) win over the `settings.yaml` defaults.

Environment variable naming:

- Prefix: `KB_`
- Nested delimiter: `__` (double underscore)
- e.g. `settings.llm.api_key` ↔ `KB_LLM__API_KEY`; `settings.search.title_boost` ↔
  `KB_SEARCH__TITLE_BOOST`
- List types use JSON: `KB_INGEST__ALLOWED_EXTENSIONS='["pdf","csv"]'`

`model_config`: `env_file=".env"`, `env_file_encoding="utf-8"`, `env_prefix="KB_"`,
`env_nested_delimiter="__"`, `extra="ignore"` (unknown keys ignored),
`yaml_file="config/settings.yaml"`.

`get_settings()` is `@lru_cache(maxsize=1)` — loaded once per process, so editing `.env`
or `settings.yaml` at runtime requires a restart to take effect (the taxonomy
`taxonomy.yaml` is the exception — hot-reloadable via an admin endpoint).

---

## `es` — Elasticsearch (`ESConfig`) {#es}

| Field | Env var | Type | Default | Notes |
|---|---|---|---|---|
| `url` | `KB_ES__URL` | str | `https://localhost:9200` | ES address. docker-compose injects `http://elasticsearch:9200` |
| `index_prefix` | `KB_ES__INDEX_PREFIX` | str | `kb` | index/alias prefix (`kb_alarm`, …) |
| `request_timeout_s` | `KB_ES__REQUEST_TIMEOUT_S` | int | `10` | per-request timeout (s) |
| `ssl_fingerprint` | `KB_ES__SSL_FINGERPRINT` | str\|None | `None` | server TLS cert SHA-256 fingerprint; set → no CA needed, works with self-signed |
| `verify_certs` | `KB_ES__VERIFY_CERTS` | bool | `True` | verify certs; set `false` for local dev |
| `username` | `KB_ES__USERNAME` | str\|None | `None` | basic-auth username |
| `password` | `KB_ES__PASSWORD` | str\|None | `None` | basic-auth password (use an env var) |
| `analyzer_index` | `KB_ES__ANALYZER_INDEX` | str | `ik_max_word` | index analyzer; set `cjk` without the IK plugin |
| `analyzer_query` | `KB_ES__ANALYZER_QUERY` | str | `ik_smart` | query analyzer; set `cjk` without the IK plugin |

Client construction (`src/kb/es/client.py`): basic auth only when both `username` and
`password` are set; with `ssl_fingerprint` it pins by fingerprint (no CA), otherwise if
`verify_certs=false` it disables cert verification.

!!! warning "settings.yaml defaults to HTTP"
    `config/settings.yaml` sets `url` to `http://localhost:9200` and `verify_certs:
    false` to match docker-compose's plaintext ES (`xpack.security.enabled=false`).
    For production switch back to HTTPS with a fingerprint and credentials.

---

## `embedding` — vector embeddings (`EmbeddingConfig`) {#embedding}

| Field | Env var | Type | Default | Range | Notes |
|---|---|---|---|---|---|
| `url` | `KB_EMBEDDING__URL` | str | `http://localhost:8080` | — | base of an OpenAI-compatible embeddings endpoint (actual request: `POST {url}/embeddings`) |
| `api_key` | `KB_EMBEDDING__API_KEY` | str | `""` | — | **empty → vector search disabled** (BM25-only) |
| `model` | `KB_EMBEDDING__MODEL` | str | `text-embedding-v3` | — | model name |
| `dims` | `KB_EMBEDDING__DIMS` | int | `1024` | — | output dimension; **must equal the model's actual dim** or writes fail |
| `batch_size` | `KB_EMBEDDING__BATCH_SIZE` | int | `10` | 1–128 | items per batch; DashScope's compatible endpoint caps at 10 |
| `timeout_s` | `KB_EMBEDDING__TIMEOUT_S` | int | `30` | — | request timeout (s) |

`settings.yaml` defaults `url` to
`https://dashscope.aliyuncs.com/compatible-mode/v1`. The client normalizes to a base
with a trailing `/` and sends the relative path `embeddings`. The response is sorted by
`index` to preserve input order, and every vector's dimension is checked. Built-in
3-attempt exponential-backoff retry (`tenacity`).

---

## `search` — search tuning (`SearchConfig`) {#search}

| Field | Env var | Type | Default | Range | Notes |
|---|---|---|---|---|---|
| `strict_max_hits` | `KB_SEARCH__STRICT_MAX_HITS` | int | `8` | 1–50 | strict hits above this → `too_many` |
| `title_boost` | `KB_SEARCH__TITLE_BOOST` | float | `3.0` | 1.0–10.0 | BM25 weight of `title` relative to `body` |
| `rrf_window` | `KB_SEARCH__RRF_WINDOW` | int | `50` | 10–500 | top recall candidates rescored with BM25+vector (rescore window) |
| `vector_weight` | `KB_SEARCH__VECTOR_WEIGHT` | float | `0.5` | 0.0–1.0 | weight of the vector score in the final blend |
| `max_result_window` | `KB_SEARCH__MAX_RESULT_WINDOW` | int | `10000` | 10–100000 | `from_+size` cap, mirrors ES `index.max_result_window` |

Final scoring formula (when embeddings are available, over the rescore window):

```
final_score = (1 - vector_weight) × BM25 + vector_weight × (cosine_sim + 1)
```

`cosine_sim + 1` maps `[-1,1]` → `[0,2]` to stay non-negative. For tuning method see
[Search & Ranking](../architecture/search-ranking.md) and
[Observability § Search feedback](../observability.md#search-feedback).

!!! note "`max_result_window` exists twice"
    The model layer `SearchRequest` validates `from_+size` against a **literal** `10000`
    (to keep the model decoupled from settings); this `max_result_window` is the
    configurable counterpart of the same cap, recorded for operators.

---

## `taxonomy` — taxonomy (`TaxonomyConfig`) {#taxonomy}

| Field | Env var | Type | Default | Notes |
|---|---|---|---|---|
| `path` | `KB_TAXONOMY__PATH` | str | `config/taxonomy.yaml` | taxonomy YAML path |

This file is rewritten at startup by `_sync_taxonomy_from_es()` (appends new values found
in ES, rewrites `version`), so it **must be writable at runtime**. Hot-reload via
`POST /api/v1/admin/reload-taxonomy`.

---

## `llm` — LLM (`LLMConfig`) {#llm}

| Field | Env var | Type | Default | Range | Notes |
|---|---|---|---|---|---|
| `api_url` | `KB_LLM__API_URL` | str | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` | — | OpenAI-compatible chat-completions endpoint |
| `api_key` | `KB_LLM__API_KEY` | str | `""` | — | **empty → `/chat`, `/extract`, import segmentation return 503** |
| `model` | `KB_LLM__MODEL` | str | `qwen-plus` | — | model name |
| `max_tokens` | `KB_LLM__MAX_TOKENS` | int | `1200` | — | default max output tokens (segmentation overrides per-call) |
| `timeout_s` | `KB_LLM__TIMEOUT_S` | int | `20` | 1–300 | default chat read-timeout (s) |
| `extract_timeout_s` | `KB_LLM__EXTRACT_TIMEOUT_S` | int | `10` | 1–300 | shorter budget for `/extract` |
| `max_retries` | `KB_LLM__MAX_RETRIES` | int | `2` | 0–5 | transient (429/5xx/timeout) retries; 0 disables |

Every provider must implement the OpenAI Chat Completions protocol; swapping providers
needs only these three settings, no code change:

```bash
KB_LLM__API_KEY=your-key
KB_LLM__API_URL=https://api.openai.com/v1/chat/completions
KB_LLM__MODEL=gpt-4o-mini
```

The client (`src/kb/services/llm.py`) treats 429 and 5xx as retryable and other 4xx as
permanent; segmentation (`src/kb/services/segmentation.py`) derives a longer read-timeout
from the message-payload size, overriding `timeout_s`.

---

## `server` — server (`ServerConfig`) {#server}

| Field | Env var | Type | Default | Range | Notes |
|---|---|---|---|---|---|
| `host` | `KB_SERVER__HOST` | str | `0.0.0.0` | — | bind address |
| `port` | `KB_SERVER__PORT` | int | `8000` | 1–65535 | bind port |

!!! note "Port reading differs by launcher"
    `python -m kb` (`src/kb/__main__.py`) reads `server.port`/`server.host` as the
    `argparse` defaults; launching `uvicorn kb.main:app` directly does **not** read
    settings — use `--port`.

---

## `ingest` — file import (`IngestConfig`) {#ingest}

| Field | Env var | Type | Default | Range | Notes |
|---|---|---|---|---|---|
| `upload_dir` | `KB_INGEST__UPLOAD_DIR` | str | `data/uploads` | — | where uploads are persisted |
| `scan_root` | `KB_INGEST__SCAN_ROOT` | str | `data` | — | root for server-side folder scans; scans confined here (path-traversal guard) |
| `max_file_size_mb` | `KB_INGEST__MAX_FILE_SIZE_MB` | int | `50` | 1–500 | per-file size cap (MB) |
| `allowed_extensions` | `KB_INGEST__ALLOWED_EXTENSIONS` | list[str] | `["pdf","xlsx","xls","csv","pptx","docx"]` | — | allowed extensions |
| `pdf_max_pages` | `KB_INGEST__PDF_MAX_PAGES` | int | `2000` | 1–50000 | PDF page cap (OOM guard) |
| `xlsx_max_cells` | `KB_INGEST__XLSX_MAX_CELLS` | int | `2000000` | ≥1000 | spreadsheet cell cap (OOM guard) |
| `ocr_enabled` | `KB_INGEST__OCR_ENABLED` | bool | `True` | — | enable the PaddleOCR fallback |
| `ocr_lang` | `KB_INGEST__OCR_LANG` | str | `ch` | — | OCR model; `ch` also reads Latin, `en` for English-only |
| `ocr_min_confidence` | `KB_INGEST__OCR_MIN_CONFIDENCE` | float | `0.5` | 0.0–1.0 | drop OCR lines below this confidence |
| `segmentation_max_tokens` | `KB_INGEST__SEGMENTATION_MAX_TOKENS` | int | `8000` | — | max segmentation LLM output tokens |
| `segmentation_chunk_chars` | `KB_INGEST__SEGMENTATION_CHUNK_CHARS` | int | `12000` | 1000–100000 | characters per LLM segmentation chunk |
| `session_ttl_minutes` | `KB_INGEST__SESSION_TTL_MINUTES` | int | `120` | 10–1440 | soft TTL: evict committed/failed sessions |
| `session_hard_ttl_minutes` | `KB_INGEST__SESSION_HARD_TTL_MINUTES` | int | `480` | 10–10080 | hard TTL: evict any session (incl. under review), bounds memory |
| `session_evict_interval_minutes` | `KB_INGEST__SESSION_EVICT_INTERVAL_MINUTES` | int | `15` | 1–1440 | background sweeper interval |

!!! warning "OCR dependencies are separate"
    `ocr_enabled=true` is only the switch; you still need PaddleOCR installed at runtime
    (the `ocr` extra, or image build arg `INSTALL_OCR=true`). Without it, a scanned PDF
    yields an actionable hint rather than crashing.

`segmentation_chunk_chars` trade-off: larger → fewer API calls but more tokens per call.
12000 chars ≈ 3000–4000 input tokens, fitting 6–10 alarm entries comfortably.

---

## `observability` — observability (`ObservabilityConfig`) {#observability}

| Field | Env var | Type | Default | Notes |
|---|---|---|---|---|
| `metrics_enabled` | `KB_OBSERVABILITY__METRICS_ENABLED` | bool | `True` | expose `GET /metrics` (Prometheus text) |
| `json_logs` | `KB_OBSERVABILITY__JSON_LOGS` | bool | `False` | `true` → one JSON object per log line |
| `log_level` | `KB_OBSERVABILITY__LOG_LEVEL` | str | `INFO` | log level |

See [Observability](../observability.md).

---

## Degradation switches at a glance {#degradation}

| Missing | Effect |
|---|---|
| `KB_LLM__API_KEY` | `/chat`, `/extract` return 503; import segmentation unavailable; search/index still work |
| `KB_EMBEDDING__API_KEY` (or service unreachable) | no vector rescore, no kNN fallback → BM25-only; server boots fine |
| ES unreachable | startup enters DEGRADED mode; search/index fail until recovery (`/readyz` returns 503) |
| No IK plugin | set `KB_ES__ANALYZER_INDEX`/`KB_ES__ANALYZER_QUERY` to `cjk` |
| No OCR deps | scanned PDFs return an actionable error; other files fine |

The full env-var template lives in `.env.example` at the repo root (every default
documented inline).
