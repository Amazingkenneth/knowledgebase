# Getting Started

There are two ways to run the project — pick the row that matches your goal.

| Goal | What you need |
|------|---------------|
| **Just run it** (deploy / try it out) | Docker + Docker Compose 24+ — see [Option A](#option-a-docker-compose-recommended). Nothing else. |
| **Develop / modify code** | Python 3.12+, [uv](https://docs.astral.sh/uv/), and Docker (for Elasticsearch) — see [Option B](#option-b-local-host-python-es-container). |

An LLM API key (`KB_LLM__API_KEY`) and an embedding key (`KB_EMBEDDING__API_KEY`)
are **optional** — the server boots without them and degrades gracefully
(keyword-only search, AI chat disabled). See [Configuration](configuration.md).

---

## Option A — Docker Compose (recommended)

Everything runs in containers. The only requirement is Docker.

```bash
# 1. Clone, then (optionally) add your API keys
cp .env.example .env          # edit .env to set KB_LLM__API_KEY / KB_EMBEDDING__API_KEY
                              # (skip this and the app still runs — keyword-only, AI chat off)

# 2. Build and start the whole stack (ES + IK plugin + API)
docker compose up -d --build  # first build ~2-3 min; subsequent starts are instant
```

Open **http://localhost:8000**.

| URL | Description |
|-----|-------------|
| `http://localhost:8000` | Knowledge Base Search UI |
| `http://localhost:8000/docs` | Swagger UI / interactive API docs |
| `http://localhost:8000/redoc` | ReDoc API reference |
| `http://localhost:9200` | Elasticsearch (direct) |

**Everyday commands:**

```bash
docker compose logs -f app        # follow API logs
docker compose ps                 # service status + health
docker compose restart app        # restart the API (e.g. after editing config/*.csv)
docker compose down               # stop everything (keeps ES data + uploads)
docker compose down -v            # stop and wipe the Elasticsearch data volume
```

What the compose stack gives you:

- The `app` service waits for Elasticsearch to be **healthy** before starting, and
  reaches it over the internal network (`KB_ES__URL=http://elasticsearch:9200` is
  set automatically — no need to configure it).
- **Persistence:** ES data lives in the `es-data` named volume; uploaded/imported
  files in `./data/uploads`; your CSVs and `taxonomy.yaml` are bind-mounted from
  `./config`, so edits on the host take effect on the next `docker compose restart app`.
- **API keys** are read from `.env` (optional). Anything in `.env` is passed through
  to the container.

!!! tip "Enabling OCR (scanned PDFs/images)"
    OCR (PaddleOCR) is left out of the default image to keep it slim (~440 MB). To
    bake it in, set the build arg in `docker-compose.yml`:

    ```yaml
    # docker-compose.yml → services.app.build.args
    INSTALL_OCR: "true"      # adds ~1.5-2 GB; models download on first use
    ```

    then rebuild: `docker compose build app && docker compose up -d`.

---

## Option B — Local (host Python + ES container)

Run the app from source for development; run Elasticsearch in a container.

```bash
# 1. Start Elasticsearch only (with the IK analyzer plugin)
docker compose up -d --build elasticsearch

# 2. Install dependencies and run the dev server with autoreload
uv run python -m kb --reload          # port from KB_SERVER__PORT (default 8000)
uv run python -m kb --port 8001 --reload   # explicit port override
```

Useful development commands:

```bash
uv run pytest tests/unit                       # unit tests (no infra)
uv run --extra ingest pytest tests/unit        # incl. PPTX/PDF extraction tests
uv run --extra ingest --extra ocr pytest tests/unit  # incl. OCR (needs libGL)
uv run pytest tests/integration -m integration # needs Docker
uv run ruff check src tests                    # lint
uv run mypy src                                # type check
```

!!! note "Test fixtures are generated, not committed"
    PPTX/PDF fixture files are produced on-the-fly by `tests/unit/conftest.py` from
    the seed CSVs — no large binary test assets live in the repo.

---

## Seeding & data

On every startup the server **clears all documents from every index and reloads
from the CSV files** under `config/`. Additions, edits, and row deletions in the
CSVs all take effect on the next restart. After seeding, `restore_imports()`
re-indexes any previously imported documents from the `kb_import_files` tracker
index — so imported files survive the re-seed. See
[Architecture → Import Pipeline](architecture/import-pipeline.md#file-tracker-kb_import_files).

---

## Reading these docs locally

This documentation site is built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/).
To serve it with live reload, or publish it:

```bash
uv sync --extra docs           # install mkdocs-material + the i18n plugin
uv run mkdocs serve            # live preview at http://127.0.0.1:8000
uv run mkdocs build --strict   # build static site into ./site (fails on broken links)
uv run mkdocs gh-deploy        # publish to the gh-pages branch (GitHub Pages)
```

The site is bilingual (English / 中文). Use the language switcher in the header.
