# Configuration

Settings are layered, highest precedence first:

```
shell env vars  →  .env  →  config/settings.yaml  →  built-in defaults
```

The schema is the pydantic-settings `Settings` class in `src/kb/config.py` — the
single source of truth for every field. `config/settings.yaml` holds runtime
defaults; `.env` (git-ignored) holds secrets and is auto-loaded by
pydantic-settings. Copy `.env.example` → `.env` and fill in values.

!!! info "Env var naming"
    All env vars use the prefix `KB_` and `__` as the nested delimiter.
    `KB_LLM__API_KEY` → `settings.llm.api_key`,
    `KB_ES__URL` → `settings.es.url`. `settings.yaml` ranks **below** env vars so a
    `KB_*` override (e.g. `KB_ES__URL` injected by docker-compose) wins over the
    file's defaults.

---

## Critical env vars

| Env var | Effect when unset |
|---|---|
| `KB_LLM__API_KEY` | `/api/v1/chat`, `/extract`, and all `/ingest/*` endpoints return **503**. Search and indexing still work. |
| `KB_EMBEDDING__API_KEY` | Vector rescore and kNN fallback are disabled — **BM25-only** keyword search. The server boots normally. |
| `KB_ES__ANALYZER_INDEX` / `KB_ES__ANALYZER_QUERY` | Default to `ik_max_word` / `ik_smart`. Set **both** to `cjk` if running ES without the IK plugin. |

---

## Configuration groups

### Elasticsearch (`es`)

| Key | Default | Notes |
|---|---|---|
| `url` | `https://localhost:9200` | docker-compose overrides to `http://elasticsearch:9200` |
| `index_prefix` | `kb` | Prefix for all indices/aliases |
| `request_timeout_s` | `10` | Per-request ES timeout |
| `ssl_fingerprint` | `null` | SHA-256 cert fingerprint for self-signed HTTPS |
| `verify_certs` | `true` | Set `false` for local dev only |
| `username` / `password` | `null` | Basic auth for secured clusters |
| `analyzer_index` | `ik_max_word` | Index-time analyzer (IK plugin); fallback `cjk` |
| `analyzer_query` | `ik_smart` | Query-time analyzer; fallback `cjk` |

### Embedding (`embedding`)

| Key | Default | Notes |
|---|---|---|
| `url` | `http://localhost:8080` | DashScope OpenAI-compatible endpoint in prod |
| `api_key` | `""` | Required to enable vector search |
| `model` | `text-embedding-v3` | 1024-dim by default |
| `dims` | `1024` | Must match the `body_vec` mapping |
| `batch_size` | `10` | DashScope rejects batches > 10 |
| `timeout_s` | `30` | Per-request timeout |

### Search (`search`) {#search}

| Key | Default | Effect |
|---|---|---|
| `strict_max_hits` | `8` | `too_many` threshold |
| `title_boost` | `3.0` | Title weight vs body in BM25 |
| `rrf_window` | `50` | Recall hits rescored by vector |
| `vector_weight` | `0.5` | BM25 ↔ cosine balance in the final score |
| `max_result_window` | `10000` | Deepest `from_ + size`; mirrors ES `index.max_result_window` |

See [Search & Ranking](architecture/search-ranking.md) for what each knob does.

### LLM (`llm`)

| Key | Default | Notes |
|---|---|---|
| `api_url` | DashScope compatible-mode URL | Any OpenAI-Chat-Completions endpoint |
| `api_key` | `""` | From `.env`; enables AI endpoints |
| `model` | `qwen-plus` | Provider model id |
| `max_tokens` | `1200` | Max tokens per completion |
| `timeout_s` | `20` | Default chat read-timeout |
| `extract_timeout_s` | `10` | Shorter budget for `/extract` |
| `max_retries` | `2` | Transient-failure retries (429 / 5xx / timeout) |

### Ingest (`ingest`) {#ingest}

| Key | Default | Effect |
|---|---|---|
| `upload_dir` | `data/uploads` | Where uploaded files are persisted (`<hash>_<name>`) |
| `scan_root` | `data` | `POST /ingest/scan` is confined to this root |
| `max_file_size_mb` | `50` | Per-file cap; oversize → FAILED |
| `allowed_extensions` | `pdf, xlsx, xls, csv, pptx, docx` | Anything else → UNSUPPORTED |
| `pdf_max_pages` | `2000` | Memory guard during extraction |
| `xlsx_max_cells` | `2_000_000` | Memory guard during extraction |
| `ocr_enabled` | `true` | When false, image-only PDF pages yield empty |
| `ocr_lang` | `ch` | PaddleOCR language pack (`ch` also reads Latin) |
| `ocr_min_confidence` | `0.5` | Drop OCR lines below this confidence |
| `segmentation_max_tokens` | `8000` | Max-tokens for segmentation calls |
| `segmentation_chunk_chars` | `12000` | Characters per LLM chunk |
| `session_ttl_minutes` | `120` | Soft TTL: evict COMMITTED/FAILED sessions |
| `session_hard_ttl_minutes` | `480` | Hard TTL: evict any session, bounding memory |
| `session_evict_interval_minutes` | `15` | Background sweeper cadence |

### Observability (`observability`) {#observability}

| Key | Default | Notes |
|---|---|---|
| `metrics_enabled` | `true` | Expose Prometheus metrics at `GET /metrics` |
| `json_logs` | `false` | One JSON object per line when true |
| `log_level` | `INFO` | Root log level |

### Server (`server`)

| Key | Default | Notes |
|---|---|---|
| `host` | `0.0.0.0` | Bind address (used by `python -m kb`) |
| `port` | `8000` | Bind port; also `KB_SERVER__PORT` |

---

## Taxonomy

`config/taxonomy.yaml` is the **single source of truth** for the filterable enums —
`knowledge_types`, `projects`, and `equipment`. It is consumed in two places:

1. **LLM prompt priming** — the extraction/segmentation prompts list valid values so
   the model stays inside the vocabulary.
2. **Index-time validation** — `project` and `equipment` are validated against it;
   unknown values are rejected.

```yaml
version: "2026-05-19-r1"
knowledge_types: [alarm, setup, experience]
projects: [Kinneret, MEM, MHK, PDX, Boston, Sonora, Yucatan, 所有项目]
equipment: [Aligner, Conveyor, FTU, Heater, Loader, ...]
```

To onboard a new project or piece of equipment, edit this file. You can reload it
**without a restart**:

```bash
curl -X POST http://localhost:8000/api/v1/admin/reload-taxonomy
```

`GET /api/v1/facets` returns the live taxonomy. Bump `version` whenever you change
the file so facet consumers can detect the change.

!!! warning "Taxonomy is rewritten at startup"
    The startup taxonomy auto-sync rewrites `config/taxonomy.yaml`, so the file must
    stay writable (the docker-compose bind-mount keeps it so). Existing documents
    referencing a removed value will fail validation on the next re-seed.

---

## Adding a new LLM provider

Override two env vars — no code changes needed (all providers must implement the
OpenAI Chat Completions API):

```bash
KB_LLM__API_KEY=your-key
KB_LLM__API_URL=https://api.openai.com/v1/chat/completions
KB_LLM__MODEL=gpt-4o-mini
```
