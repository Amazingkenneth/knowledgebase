# CLAUDE.md — AI Agent Reference

> Quick-start for AI agents working on this codebase.
> Human documentation lives in README.md.

---

## What this project is

A **zero-fabrication manufacturing knowledge-base API**. Documents are retrieved verbatim from Elasticsearch — the system never generates text. The LLM is used as a query-understanding proxy (`/api/v1/extract`) and a conversational search assistant (`/api/v1/chat`) that extracts params, searches the KB, and answers based on results.

Stack: FastAPI · Elasticsearch 8.x (IK analyzer plugin) · pydantic-settings · httpx · DashScope Embeddings API (optional).

---

## Commands

```bash
uv run python -m kb --reload           # start dev server (port from KB_SERVER__PORT, default 8000)
uv run python -m kb --port 8001 --reload  # explicit port override
uv run uvicorn kb.main:app --reload --port 8000  # direct uvicorn (port not read from settings)
uv run pytest tests/unit               # unit tests (no infra)
uv run pytest tests/integration -m integration  # needs Docker
uv run ruff check src tests            # lint
uv run mypy src                        # type check
```

---

## Configuration system

Settings are layered: `config/settings.yaml` → `.env` → shell env vars.

- Schema: `src/kb/config.py` — pydantic-settings `Settings` class.
- `pydantic-settings` auto-loads `.env` (the file is git-ignored).
- All env vars use prefix `KB_` and `__` as the nested delimiter.
- Example: `KB_LLM__API_KEY` → `settings.llm.api_key`.
- Template: `.env.example` (committed) — copy to `.env` and fill in values.

**Critical env vars**:
- `KB_LLM__API_KEY` — required to enable AI chat endpoints. Without it, `/api/v1/chat` and `/api/v1/extract` return HTTP 503; all search/indexing still works.
- `KB_EMBEDDING__API_KEY` — required to enable vector search. Without it, the server falls back to BM25-only keyword search.
- `KB_ES__ANALYZER_INDEX` / `KB_ES__ANALYZER_QUERY` — defaults to `ik_max_word`/`ik_smart`. Set both to `cjk` if running ES without the IK plugin.

---

## Key files

| Path | Role |
|------|------|
| `src/kb/config.py` | Pydantic settings schema — single source of truth for all config fields |
| `src/kb/main.py` | FastAPI app factory + startup lifespan (index creation, CSV seeding) |
| `src/kb/api/chat.py` | Conversational search (`/chat`: extract → search → LLM) and `/extract` |
| `src/kb/api/search.py` | `POST /api/v1/search` handler |
| `src/kb/services/search.py` | Hybrid search pipeline: strict → loose → vector (RRF) |
| `src/kb/services/seed.py` | CSV → ES seeder — clears and reloads all indices on every startup |
| `src/kb/es/body_builder.py` | Builds the ES `body` text field from document sections |
| `src/kb/es/mappings.py` | Index mappings (dense_vector, keyword, text) |
| `config/settings.yaml` | Runtime defaults (ES URL, embedding, search tuning) |
| `config/taxonomy.yaml` | Valid projects / equipment / knowledge_types — edit to extend |
| `.env.example` | All supported env vars with defaults — template for `.env` |
| `elasticsearch/Dockerfile` | Custom ES image — installs the `analysis-ik` plugin at build time |

---

## Architecture constraints

- **No hallucination**: never add LLM-generated text to search responses. Results are verbatim documents or nothing.
- **Taxonomy enforcement**: `project` and `equipment` values are validated against `taxonomy.yaml` at index time. New values require a taxonomy update + re-seed.
- **Always-reseed on startup**: `seed` clears all documents from every index and reloads from the CSV files on every server start. Additions, edits, and row deletions in the CSVs all take effect automatically on the next restart.
- **BM25-only fallback**: the embedding service is optional. If it's unreachable, the server continues with keyword-only search (no kNN).

---

## Search status contract

Every `POST /api/v1/search` response carries a `status` field. **Do not change these values** — upstream callers depend on them:

| Status | Meaning |
|--------|---------|
| `strict_hit` | All filters + AND-keywords matched, within `strict_max_hits` |
| `too_many` | Strict matched more than `strict_max_hits` — caller should ask user to narrow |
| `loose_hit` | Fell back to OR-keywords — show with "for reference only" banner |
| `vector_only` | Only vector similarity matched — low confidence |
| `no_hit` | Nothing matched |

---

## Adding a new LLM provider

Override two env vars only — no code changes needed (all providers must implement the OpenAI Chat Completions API):

```bash
KB_LLM__API_KEY=your-key
KB_LLM__API_URL=https://api.openai.com/v1/chat/completions
KB_LLM__MODEL=gpt-4o-mini
```
