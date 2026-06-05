# Deployment {#deployment}

The supported deployment is **Docker Compose**: a custom Elasticsearch image (with the IK
analyzer) plus the FastAPI app image. This page documents the compose stack, the app
image, production hardening, data persistence/backup, and the docs auto-publish hook.

---

## Quick start {#quickstart}

```bash
cp .env.example .env          # fill in KB_LLM__API_KEY / KB_EMBEDDING__API_KEY (optional)
docker compose up -d --build  # builds ES+IK and the app; app on :8000
curl localhost:8000/readyz    # 200 once ES is reachable
```

`docker compose up -d --build elasticsearch` brings up **only** ES (useful when running
the app on the host via `uv run python -m kb`). Missing API keys merely disable the
matching features (no LLM → chat/extract/ingest return 503; no embedding key → BM25-only).

---

## The compose stack {#compose}

`docker-compose.yml` defines two services.

### `elasticsearch` (container `kb-es`)

- Built from `elasticsearch/Dockerfile` — the official `elasticsearch:8.15.3` image with
  the `analysis-ik` plugin installed at build time.
- `discovery.type=single-node`, `xpack.security.enabled=false`, heap pinned to
  `-Xms1g -Xmx1g`.
- Port `9200` published; data on the named volume `es-data`
  (`/usr/share/elasticsearch/data`) so it survives `docker compose down`/recreation.
- Healthcheck polls `_cluster/health` for `green`/`yellow` (5s interval, 30 retries).

### `app` (container `kb-app`)

- Built from the root `Dockerfile` with build arg `INSTALL_OCR` (default `"false"`).
- `depends_on: elasticsearch` with `condition: service_healthy` — the app waits for ES.
- Reads `.env` (optional) for API keys, and **overrides** `KB_ES__URL=http://elasticsearch:9200`
  so it reaches ES over the compose network regardless of any `KB_ES__URL` in `.env`.
- Port `8000` published; `restart: unless-stopped`.
- Bind mounts (host → container):
  - `./config` → `/app/config` — so seed-CSV edits **and** the startup taxonomy auto-sync
    (which rewrites `config/taxonomy.yaml`) persist to the host. This dir must stay
    writable.
  - `./data/uploads` → `/app/data/uploads` — so uploaded/imported files survive container
    recreation.
- Healthcheck: `curl -fs http://localhost:8000/readyz` (15s interval, 40s start period).

!!! warning "Runtime assets are read relative to the working dir"
    The image sets `WORKDIR /app` and the app reads `config/`, the seed CSVs,
    `data/uploads`, and `Knowledge Base Search.html` relative to it. Keep those paths in
    place (compose bind-mounts the first two).

---

## The app image {#image}

`Dockerfile` is a multi-stage `uv` build:

- **Builder** (`python:3.12-slim`) — installs deps reproducibly from `uv.lock` with the
  `ingest` extra baked in (PDF/XLSX/PPTX/DOCX import works out of the box), then installs
  the project as a non-editable wheel into `/app/.venv`.
- **Optional OCR** — `--build-arg INSTALL_OCR=true` adds `paddleocr` + `paddlepaddle`
  (~1.5–2 GB) and the extra runtime libs `libgl1` / `libglib2.0-0`. Off by default to keep
  the image lean.
- **Runtime** (`python:3.12-slim`) — copies the venv, `config/`, and the frontend HTML;
  uses `tini` as PID 1 for clean signal handling; `HEALTHCHECK` hits `/readyz`; entrypoint
  `python -m kb --host 0.0.0.0 --port 8000`.

```bash
docker build -t kb-app .                               # slim (no OCR)
docker build -t kb-app --build-arg INSTALL_OCR=true .  # with PaddleOCR
```

To enable OCR in compose, set `args.INSTALL_OCR: "true"` under the `app` service and
rebuild.

---

## Production hardening {#hardening}

The default compose stack is a **single-node, security-off** dev setup. For anything
beyond local use:

| Concern | What to do | Setting / mechanism |
|---|---|---|
| **ES over TLS** | Point the app at an HTTPS ES and verify certs | `KB_ES__URL=https://…`, `KB_ES__VERIFY_CERTS=true`; for a self-signed node set `KB_ES__SSL_FINGERPRINT` (SHA-256 from ES startup output) |
| **ES auth** | Enable `xpack.security`, create a least-privilege user | `KB_ES__USERNAME` / `KB_ES__PASSWORD` |
| **Secrets** | Never bake keys into the image | Mount `.env` or inject `KB_LLM__API_KEY` / `KB_EMBEDDING__API_KEY` via your orchestrator's secret store |
| **AuthN/Z** | The app has none — put it behind a gateway | Reverse proxy / API gateway enforcing auth (see [Security](security.md)) |
| **Heap / resources** | 1 GB heap is dev-sized | Raise `ES_JAVA_OPTS`; size ES to your corpus |
| **Scaling** | App is stateless except in-memory import sessions | Run multiple app replicas behind a load balancer; note import-session preview state is per-process (see below) |

!!! note "Import sessions are in-process"
    Staged import sessions live in memory in the app process (TTL-evicted). Behind a
    load balancer, pin an import review flow to one replica (sticky sessions) or scale the
    app to one replica for the ingest UI — committed documents and the file tracker are in
    ES and are shared, only the *preview* state is local.

---

## Data persistence & backup {#backup}

Three kinds of state, all recoverable:

1. **Search documents in ES** — the source of truth for *imported* docs is the
   `kb_import_files` tracker index plus the seed CSVs. On every startup the app **clears
   and reseeds** the main indices from the CSVs, then `restore_imports()` replays committed
   imports from the tracker (see
   [Build from Scratch → Startup](../reference/build-from-scratch.md#startup)).
2. **Seed CSVs + `config/`** — version-control these; they fully define the seeded corpus.
3. **The `es-data` volume** — back it up if you want point-in-time recovery without a
   reseed:

```bash
# Snapshot the ES data volume to a tarball
docker run --rm -v knowledgebase_es-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/es-data-$(date +%F).tgz -C /data .

# Restore into a fresh volume
docker run --rm -v knowledgebase_es-data:/data -v "$PWD":/backup alpine \
  sh -c "cd /data && tar xzf /backup/es-data-2026-06-05.tgz"
```

For larger deployments prefer the Elasticsearch
[snapshot API](https://www.elastic.co/guide/en/elasticsearch/reference/current/snapshot-restore.html)
to a shared repository over a raw volume copy.

---

## Docs auto-publish {#docs-publish}

The documentation site (this site) publishes to GitHub Pages via a **local `pre-push` git
hook** — deliberately not GitHub Actions, to avoid CI cost.

- Hooks are version-controlled in `scripts/git-hooks/` and activated by
  `./scripts/install-hooks.sh` (which sets `core.hooksPath`, and is why that dir also
  carries the repo's Git LFS passthrough hooks: `post-checkout`, `post-commit`,
  `post-merge`).
- On a push to `main` that touches `docs/` or `mkdocs.yml`, the `pre-push` hook runs
  `scripts/deploy-docs.sh`, which does `mkdocs gh-deploy --strict` (build + push to
  `gh-pages`). A failed strict build **aborts the push**.
- Run `scripts/deploy-docs.sh` directly to publish on demand; bypass the hook once with
  `git push --no-verify`.

Build/preview locally first:

```bash
uv sync --extra docs
uv run mkdocs serve              # preview EN/中文 at :8000
uv run mkdocs build --strict     # fails on broken links/anchors; output → ./site (git-ignored)
```
