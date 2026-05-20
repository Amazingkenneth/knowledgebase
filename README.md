# Knowledge Base — Manufacturing Search Engine

A precision information-retrieval service for manufacturing knowledge. Built on Elasticsearch with hybrid BM25 + vector search — not RAG, not generative AI. Documents are returned verbatim or not at all.

> **Why not RAG?** In manufacturing, alarm codes differ by one character, equipment parameters are meaningless without domain context, and wrong answers have real consequences. This system is designed around a zero-fabrication guarantee: if a document matches, it is shown as-is; if nothing matches, the caller is told so explicitly.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Quickstart](#quickstart)
- [Data Source — CSV Files](#data-source--csv-files)
- [Configuration](#configuration)
- [AI Chat API](#ai-chat-api)
- [Taxonomy](#taxonomy)
- [Document Types](#document-types)
- [API Reference](#api-reference)
- [Search Behaviour](#search-behaviour)
- [Running Tests](#running-tests)
- [Project Structure](#project-structure)

---

## Architecture Overview

```
┌─────────────────────────────────────────┐
│          Upstream Chat Layer            │  ← extracts structured params from NL
└────────────────┬────────────────────────┘
                 │ SearchRequest (structured)
                 ▼
┌─────────────────────────────────────────┐
│         FastAPI (kb.main)               │
│                                         │
│  POST /api/v1/search                    │
│  POST /api/v1/documents/{type}          │
│  GET  /api/v1/facets                    │
└──────┬──────────────┬───────────────────┘
       │              │
       ▼              ▼
┌──────────┐   ┌───────────────────────────┐
│  ES 8.x  │   │ Text-Embeddings-Inference │
│ (BM25 +  │   │     bge-m3 (1024-dim)     │
│  kNN)    │   └───────────────────────────┘
└──────────┘
```

**Retrieval strategy**: structured filters narrow the candidate set first, then hybrid BM25 keyword search + dense vector similarity re-ranks results using Reciprocal Rank Fusion (RRF). The caller never sees AI-generated text — only verbatim document sections.

---

## Prerequisites

| Tool | Version |
|------|---------|
| Python | 3.12+ |
| [uv](https://docs.astral.sh/uv/) | latest |
| Docker + Docker Compose | 24+ |

---

## Quickstart

### 1. Install Python dependencies

```bash
uv sync
```

Or with plain pip:

```bash
pip install -e .
```

For dev extras (testing, linting):

```bash
uv sync --extra dev
# or: pip install -e ".[dev]"
```

### 2. Start Elasticsearch

```bash
docker compose up -d elasticsearch
```

This starts `kb-es` — Elasticsearch 8.15.3 on port 9200 with security disabled (plain HTTP, no auth). Wait until healthy:

```bash
curl -s http://localhost:9200/_cluster/health | python3 -m json.tool
# "status": "green" or "yellow" means ready
```

> **Embedding service (optional):** `docker compose up -d embedding` starts the HuggingFace TEI (bge-m3) service on port 8080. It downloads ~1 GB on first start. Without it the server runs in **BM25-only mode** — keyword search works fully, kNN semantic search is disabled until the embedding service is available.

### 3. (Optional) Install the IK Chinese analyzer

For better Chinese tokenization, install the IK plugin once after ES starts:

```bash
docker exec -it kb-es bin/elasticsearch-plugin install \
  https://release.infinilabs.com/analysis-ik/stable/elasticsearch-analysis-ik-8.15.3.zip
docker restart kb-es
```

Skip this step if you don't need improved Chinese tokenization. The built-in `cjk` analyzer works without any plugin.

### 4. Start the API server

```bash
uv run uvicorn kb.main:app --reload
```

On first startup the server automatically:

1. Creates Elasticsearch indices (`kb_alarm`, `kb_setup`, `kb_experience`) if they don't exist
2. Reads the three CSV files in `config/` and seeds all documents into ES (skipped if indices are already populated)
3. Serves the frontend at `http://localhost:8000`

| URL | Description |
|-----|-------------|
| `http://localhost:8000` | Knowledge Base Search UI |
| `http://localhost:8000/docs` | Swagger UI / interactive API docs |
| `http://localhost:8000/redoc` | ReDoc API reference |

### Troubleshooting startup

If the server fails to connect to ES, check `config/settings.yaml`:

```yaml
es:
  url: "http://localhost:9200"   # plain HTTP, no auth
```

The ES container in `docker-compose.yml` runs with `xpack.security.enabled=false`.

---

## Data Source — CSV Files

The system loads its knowledge base from three CSV files in `config/`. These are the authoritative corporate data source; the server reads and indexes them automatically on startup (only when the Elasticsearch index is empty).

### File overview

| File | Type | ES index | Rows (current) |
|------|------|----------|----------------|
| `机台报警_header.csv` | Machine alarms | `kb_alarm` | 100 |
| `机台setup_header.csv` | Equipment setup / calibration | `kb_setup` | 100 |
| `设备经验_header.csv` | Field experience / failure cases | `kb_experience` | 100 |

### Column mapping

**`机台报警_header.csv` → alarm documents**

| CSV column | ES field | Notes |
|---|---|---|
| 项目 | `project` | Must match a value in `taxonomy.yaml` |
| 机台 | `equipment` | Must match a value in `taxonomy.yaml` |
| 代码 | `error_codes` | Numeric or alphanumeric (e.g. `120001`, `SP-042`) |
| 中文标题 | `title` (prefix) | Combined with 英文标题 as `"中文（英文）"` |
| 英文标题 | `title` (suffix) | |
| 内容 | `content` | Alarm description |
| 解除流程 | `resolution` | Step-by-step resolution |
| 注意事项 | `notes` | Warnings |
| ppt文件 | `source_file` | Source document filename |
| ppt页面 | `source_pages` | Page number(s) |

**`机台setup_header.csv` → setup documents**

| CSV column | ES field | Notes |
|---|---|---|
| 项目 | `project` | |
| 设备 | `equipment` | |
| 工站/部件/站位 | `title` | Auto-generated: `"{设备} · {station} 调试"` |
| 规格/要求 | `prerequisites` | First line |
| 调试工具 | `prerequisites` | Second line (appended) |
| 调试步骤 | `procedure` | Setup steps |
| 注意事项 | `notes` | |
| ppt文件 | `source_file` | |
| PPT页面 | `source_pages` | |

**`设备经验_header.csv` → experience documents**

| CSV column | ES field | Notes |
|---|---|---|
| 项目 | `project` | |
| 机台 | `equipment` | |
| 问题 | `title` | |
| 失败描述 | `body_text` | Opening paragraph |
| 失败分析 | `body_text` | Appended as `【失败分析】…` |
| 根因 | `body_text` | Appended as `【根因】…` |
| 纠正步骤 | `procedure` | Corrective actions |
| PPT文件 | `source_file` | |
| PPT页面 | `source_pages` | |

### How to update the knowledge base

1. Edit one or more of the three CSV files (keep the header row unchanged).
2. Add any new projects or equipment names to `config/taxonomy.yaml` and reload:
   ```bash
   curl -X POST http://localhost:8000/api/v1/admin/reload-taxonomy
   ```
3. Delete the affected ES index (the server will re-seed on next restart):
   ```bash
   # Find the concrete index name behind the alias
   curl http://localhost:9200/_alias/kb_alarm
   # Delete it (replace kb_alarm_v1 with the actual name)
   curl -X DELETE http://localhost:9200/kb_alarm_v1
   ```
4. Restart the server:
   ```bash
   uv run uvicorn kb.main:app --reload
   ```

> **Duplicate rows**: rows that produce identical content hash (same title + content + project + equipment) are deduplicated automatically — only one copy is stored in ES.

> **Missing CSV**: if a CSV file is absent, the server logs a warning and skips that document type.

---

## Configuration

Settings are loaded in priority order: `config/settings.yaml` → `.env` (auto-loaded, git-ignored) → shell environment variables. Use `.env.example` as a starting template.

```yaml
# config/settings.yaml
es:
  url: "http://localhost:9200"   # plain HTTP; no auth for local dev
  index_prefix: "kb"
  request_timeout_s: 10
  verify_certs: false
  # For production with TLS + auth, uncomment:
  # url: "https://my-cluster:9200"
  # username: "elastic"
  # password: "..."             # or use KB_ES__PASSWORD env var
  # ssl_fingerprint: "dfbe360e..."

embedding:
  url: "http://localhost:8080"   # TEI service; server works without it (BM25-only)
  model: "BAAI/bge-m3"
  dims: 1024
  batch_size: 32
  timeout_s: 30

search:
  strict_max_hits: 8             # results above this → TOO_MANY, not shown
  title_boost: 3.0               # title field weight vs body (BM25)
  rrf_window: 50
  rrf_rank_constant: 60

taxonomy:
  path: "config/taxonomy.yaml"

llm:
  api_url: "https://api.deepseek.com/v1/chat/completions"   # default: DeepSeek
  model: "deepseek-chat"
  max_tokens: 1200
  api_key: ""   # leave empty here — set KB_LLM__API_KEY in the environment instead
```

### Common env-var overrides

Environment variables use the `KB_` prefix and `__` as the nesting delimiter:

```bash
KB_ES__URL=https://my-cluster:9200
KB_ES__PASSWORD=secret
KB_ES__SSL_FINGERPRINT=dfbe360e...   # SHA-256 of the server TLS cert
KB_EMBEDDING__URL=http://gpu-host:8080
KB_LLM__API_KEY=sk-...               # required to enable AI chat features
KB_LLM__API_URL=https://api.openai.com/v1/chat/completions   # switch provider
KB_LLM__MODEL=gpt-4o-mini
KB_LLM__MAX_TOKENS=1200
```

### TLS fingerprint (production)

To get the fingerprint of your Elasticsearch TLS certificate:

```bash
openssl s_client -connect localhost:9200 -showcerts 2>/dev/null \
  | openssl x509 -fingerprint -sha256 -noout
```

---

## AI Chat API

The server includes a **LLM proxy layer** that keeps API keys server-side and away from the browser. Two endpoints are exposed:

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/chat` | Forward a conversation to the configured LLM |
| `POST /api/v1/extract` | Extract structured search parameters from a free-text query using the LLM, primed with the live taxonomy |

### Default provider — DeepSeek

Out of the box the server points at **DeepSeek** (`deepseek-chat`), which implements the OpenAI Chat Completions API wire format:

```yaml
# config/settings.yaml
llm:
  api_url: "https://api.deepseek.com/v1/chat/completions"
  model: "deepseek-chat"
  max_tokens: 1200
  api_key: ""   # set via KB_LLM__API_KEY — never commit a real key
```

Get a key at [platform.deepseek.com](https://platform.deepseek.com).

### Setting your API key

Copy `.env.example` to `.env` (git-ignored) and set your key:

```bash
cp .env.example .env
# then edit .env — only KB_LLM__API_KEY is required
```

`.env` is **loaded automatically** by `pydantic-settings` on server startup — no `source` or wrapper command needed:

```bash
uv run uvicorn kb.main:app --reload   # .env is read automatically
```

You can still override any variable inline or via the shell:

```bash
# One-off inline override (takes precedence over .env)
KB_LLM__API_KEY=sk-... uv run uvicorn kb.main:app --reload
```

> `pydantic-settings` uses `env_prefix="KB_"` and `env_nested_delimiter="__"`. Shell exports always win over `.env` values.

### Switching to a different AI provider

Any provider that implements the **OpenAI Chat Completions API** (`POST /v1/chat/completions`) works without code changes. Override the URL and model via environment variables:

| Provider | `KB_LLM__API_URL` | `KB_LLM__MODEL` |
|----------|-------------------|-----------------|
| **DeepSeek** *(default)* | `https://api.deepseek.com/v1/chat/completions` | `deepseek-chat` |
| **OpenAI** | `https://api.openai.com/v1/chat/completions` | `gpt-4o-mini` |
| **Azure OpenAI** | `https://<resource>.openai.azure.com/openai/deployments/<deployment>/chat/completions?api-version=2024-08-01-preview` | *(set by deployment)* |
| **Ollama** (local) | `http://localhost:11434/v1/chat/completions` | `qwen2.5:7b` |
| Any OpenAI-compat | your endpoint | your model name |

**Example — switch to OpenAI gpt-4o-mini:**

```bash
export KB_LLM__API_KEY=sk-your-openai-key
export KB_LLM__API_URL=https://api.openai.com/v1/chat/completions
export KB_LLM__MODEL=gpt-4o-mini
uv run uvicorn kb.main:app --reload
```

### All LLM environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KB_LLM__API_KEY` | *(empty)* | API key for the provider. **Required** to enable AI chat features. |
| `KB_LLM__API_URL` | `https://api.deepseek.com/v1/chat/completions` | Chat completions endpoint URL. |
| `KB_LLM__MODEL` | `deepseek-chat` | Model name passed to the provider in the request body. |
| `KB_LLM__MAX_TOKENS` | `1200` | Maximum tokens in the LLM response. |

### Behaviour when no API key is configured

If `KB_LLM__API_KEY` is not set:

- `POST /api/v1/chat` returns **HTTP 503** with `"LLM not configured"`.
- `POST /api/v1/extract` returns **HTTP 503** — the frontend silently falls back to its built-in rule-based parameter parser, so full-text search continues to work.
- All document retrieval and indexing endpoints are completely unaffected.

---

## Taxonomy

`config/taxonomy.yaml` is the single source of truth for valid filter values. Unknown values are rejected at index time with HTTP 400.

```yaml
version: "2026-05-19-r1"

knowledge_types:
  - alarm        # 机台报警
  - setup        # 机台 setup / 调试规范
  - experience   # 设备经验 / 故障案例

projects:
  - Kinneret
  - MEM
  - MHK
  - PDX
  - Boston
  - Sonora
  - Yucatan
  - 所有项目    # cross-project documents

equipment:
  - Aligner
  - Conveyor
  - FTU
  - Heater
  - Loader
  - Pump
  - SensorModule
  - Stage
```

**To add a new project or equipment**: edit `taxonomy.yaml`, bump `version`, reload, then re-seed:

```bash
# 1. Reload taxonomy (no restart required)
curl -X POST http://localhost:8000/api/v1/admin/reload-taxonomy

# 2. If you also added new CSV rows: delete the affected index and restart
#    (see "How to update the knowledge base" above)
```

`GET /api/v1/facets` returns the live taxonomy — upstream systems call this on startup to know the valid filter values.

---

## Document Types

Every document has common base fields:

| Field | Type | Description |
|-------|------|-------------|
| `knowledge_type` | enum | `alarm` \| `setup` \| `experience` |
| `project` | string | Project code from taxonomy |
| `equipment` | string | Equipment name from taxonomy |
| `error_codes` | string[] | Optional alarm/error codes (`[A-Z0-9][A-Z0-9_-]{0,63}`) |
| `title` | string | Max 200 chars; boosted 3× in BM25 |
| `source_file` | string? | Source document filename |
| `source_pages` | string[] | Page references in source doc |

### AlarmDoc (`knowledge_type: alarm`)

| Field | Required | Description |
|-------|----------|-------------|
| `content` | yes | Alarm description and context |
| `resolution` | yes | Step-by-step resolution procedure |
| `notes` | no | Warnings and additional notes |

### SetupDoc (`knowledge_type: setup`)

| Field | Required | Description |
|-------|----------|-------------|
| `procedure` | yes | Setup steps |
| `prerequisites` | no | Required conditions before setup |
| `notes` | no | Warnings and additional notes |

### ExperienceDoc (`knowledge_type: experience`)

| Field | Required | Description |
|-------|----------|-------------|
| `body_text` | yes | Free-form experience content |
| `procedure` | no | Step-by-step procedure (if applicable) |
| `notes` | no | Warnings and additional notes |

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Knowledge Base Search frontend (HTML) |
| `GET` | `/healthz` | Liveness check |
| `GET` | `/api/v1/facets` | Return live taxonomy (projects, equipment, types) |
| `GET` | `/api/v1/documents/stats` | Aggregate document counts by type/project/equipment |
| `POST` | `/api/v1/admin/reload-taxonomy` | Hot-reload `taxonomy.yaml` without restart |
| `POST` | `/api/v1/search` | Search documents (hybrid BM25 + kNN) |
| `POST` | `/api/v1/documents/{knowledge_type}` | Index a single document |
| `POST` | `/api/v1/documents/{knowledge_type}/_bulk` | Index multiple documents |
| `DELETE` | `/api/v1/documents/{knowledge_type}/{doc_id}` | Delete a document |

Full schema available at `http://localhost:8000/docs` (Swagger UI) or `http://localhost:8000/redoc`.

### Index a document

```bash
curl -X POST http://localhost:8000/api/v1/documents/alarm \
  -H "Content-Type: application/json" \
  -d '{
    "project": "MEM",
    "equipment": "Sphere",
    "error_codes": ["125002", "124000"],
    "title": "穿梭真空感应失败",
    "content": "穿梭真空报警分为两种...",
    "resolution": "1. 确认对应报警穿梭穴位...",
    "notes": "注意: 操作前先确认安全状态"
  }'
```

### Search

```bash
curl -X POST http://localhost:8000/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{
    "knowledge_type": "alarm",
    "project": "MEM",
    "equipment": "Sphere",
    "error_codes": ["125002"],
    "keywords": ["真空", "穿梭"],
    "query_text": "穿梭真空感应失败怎么处理",
    "mode": "auto"
  }'
```

---

## Search Behaviour

### Search modes

| Mode | Keyword logic | Use when |
|------|--------------|----------|
| `strict` | AND — document must contain all keywords | Default; precise queries |
| `loose` | OR — document needs at least one keyword | Fallback; broader recall |
| `vector_only` | No keyword filter; pure vector similarity | Semantic queries with no exact terms |
| `auto` | Tries strict → loose → vector in sequence | Default mode |

### Response status contract

The `status` field tells the caller **how to render** the results. It is a required contract — callers must honor it:

| Status | Meaning | Required UI behaviour |
|--------|---------|----------------------|
| `strict_hit` | All filters and AND-keywords matched, within threshold | Show results as authoritative |
| `too_many` | Strict matched > `strict_max_hits` | Do not show docs; prompt user to narrow filters |
| `loose_hit` | Fell back to OR-keywords | Show with **"仅供参考"** banner (for reference only) |
| `vector_only` | Only vector similarity matched | Show with low-confidence banner |
| `no_hit` | Nothing matched | Inform user; no results |

### Round-trip parameter echo

Every response includes `effective_params` — the normalized filter values actually applied. The upstream chat layer should display this to the user (e.g., "您询问 MEM 项目、Sphere 机台…") so they can immediately catch any misextraction.

When `status == too_many`, the response also includes `facets` — hit counts by project/equipment — so the caller can suggest which dimension to narrow.

---

## Running Tests

```bash
# Unit tests — fast, no infrastructure required
uv run pytest tests/unit

# Integration tests — requires Docker (Elasticsearch via testcontainers)
uv run pytest tests/integration -m integration

# All tests
uv run pytest

# Lint
uv run ruff check src tests

# Type check
uv run mypy src
```

---

## Project Structure

```
knowledgebase/
├── config/
│   ├── settings.yaml            # Runtime config — ES URL, embedding, search tuning
│   ├── taxonomy.yaml            # Valid projects / equipment / types (edit to extend)
│   ├── 机台报警_header.csv       # Machine alarm documents (100 rows)
│   ├── 机台setup_header.csv      # Equipment setup / calibration documents (100 rows)
│   └── 设备经验_header.csv       # Field experience / failure case documents (100 rows)
├── src/kb/
│   ├── main.py                  # FastAPI app + lifespan (creates indices, seeds from CSV)
│   ├── config.py                # Pydantic settings (settings.yaml + KB_* env vars)
│   ├── api/
│   │   ├── documents.py         # Index / delete / stats endpoints
│   │   ├── search.py            # POST /api/v1/search
│   │   ├── facets.py            # GET /api/v1/facets + taxonomy reload
│   │   └── deps.py              # FastAPI dependency injection
│   ├── models/
│   │   ├── document.py          # AlarmDoc, SetupDoc, ExperienceDoc (Pydantic v2)
│   │   ├── search.py            # SearchRequest, SearchResponse, SearchStatus
│   │   └── taxonomy.py          # Taxonomy, KnowledgeType
│   ├── services/
│   │   ├── csv_loader.py        # CSV files → KnowledgeDoc list
│   │   ├── seed.py              # Idempotent startup seeder (reads CSV, skips if populated)
│   │   ├── indexing.py          # Document validation + ES bulk indexing
│   │   ├── search.py            # Hybrid search pipeline (strict → loose → vector)
│   │   ├── embedding.py         # TEI client (bge-m3); graceful BM25-only fallback
│   │   └── taxonomy.py          # TaxonomyStore with hot-reload
│   └── es/
│       ├── client.py            # Async Elasticsearch client factory
│       ├── mappings.py          # Index mappings (dense_vector + keyword + text fields)
│       ├── body_builder.py      # Builds the ES `body` field from document sections
│       └── migrations.py        # Index create / delete CLI
├── tests/
│   ├── unit/                    # Pure Python, no infrastructure required
│   └── integration/             # Requires Docker (testcontainers + Elasticsearch)
├── Knowledge Base Search.html   # Single-file React frontend (served at GET /)
├── .env.example                 # Template for .env — copy and fill in KB_LLM__API_KEY
├── .gitattributes               # Enforce LF line endings for all text files
├── docker-compose.yml           # Elasticsearch 8.15.3 + optional TEI embedding service
├── CLAUDE.md                    # AI agent quick-reference (architecture, commands, constraints)
└── pyproject.toml               # Python dependencies and tool config (uv / pip)
```
