# Troubleshooting {#troubleshooting}

A symptom → cause → fix matrix for the failures you're most likely to hit, grouped by
subsystem. For the metrics and health probes referenced here, see
[Observability](../observability.md).

---

## Startup & Elasticsearch {#startup}

| Symptom | Cause | Fix |
|---|---|---|
| `GET /readyz` returns **503**, logs say *"Elasticsearch unreachable … DEGRADED mode"* | App started before ES was ready, or `KB_ES__URL` is wrong | Wait for ES health (compose `depends_on: service_healthy` handles this); verify `KB_ES__URL`. The app starts degraded and recovers when ES comes up — search/index fail until then |
| Indexing fails with an **analyzer not found** / mapping error | ES has no `analysis-ik` plugin but mappings ask for `ik_max_word`/`ik_smart` | Use the bundled `elasticsearch/Dockerfile` (installs IK), **or** set both `KB_ES__ANALYZER_INDEX` and `KB_ES__ANALYZER_QUERY` to `cjk` (built-in bigram analysis) |
| TLS handshake / cert errors talking to ES | HTTPS ES with a self-signed cert | Set `KB_ES__SSL_FINGERPRINT` (SHA-256 from ES startup output), or `KB_ES__VERIFY_CERTS=false` for local dev only |

---

## Search & ranking {#search}

| Symptom | Cause | Fix |
|---|---|---|
| Results return but are clearly **keyword-only** (no semantic matches); `kb_upstream_errors_total{service="embedding"}` rising | Embedding service down/unset — search silently fell back to BM25 | Set/repair `KB_EMBEDDING__API_KEY` + `KB_EMBEDDING__URL`; check `GET /readyz?deep=true` reports `embedding: ok` |
| Every vector search errors after enabling embeddings | `KB_EMBEDDING__DIMS` ≠ the model's real dimension; ES rejects the `dense_vector` | Set `dims` to the model's output (default `1024` for `text-embedding-v3`); a dimension change requires a **re-seed** (indices are recreated) |
| Status is always `too_many` | More than `strict_max_hits` (default 8) strict matches | Expected — narrow the query, or raise `KB_SEARCH__STRICT_MAX_HITS` |
| `POST /api/v1/search` returns **422** | `from_ + size > 10000`, or a field exceeds its bound | Page less deeply (the cap mirrors ES `index.max_result_window`); narrow instead of paging |
| Filtering on a project/equipment returns nothing | The value isn't in the taxonomy (so the LLM-supplied value was dropped, or you filtered on an unknown literal) | Check `GET /api/v1/facets`; add the value to `config/taxonomy.yaml` and reload |

---

## AI chat & extract {#chat}

| Symptom | Cause | Fix |
|---|---|---|
| `/chat` or `/extract` returns **503** "LLM not configured" | `KB_LLM__API_KEY` unset | Set the key; restart or it's read at call time depending on deployment |
| `/extract` returns **502** "unparseable response" | LLM returned non-JSON / malformed output | Transient — retried automatically (`KB_LLM__MAX_RETRIES`); if persistent, check the model supports JSON-ish output and `KB_LLM__MODEL` is valid |
| `/chat` answer says retrieval is unavailable (`search_error: true`) | The KB search raised (e.g. ES down) — **not** a genuine no-hit | Fix ES; the model is deliberately told retrieval is down rather than answering from nothing |
| Extracted `equipment`/`project` keeps coming back `null` | The extractor only fills taxonomy values the user explicitly names; "宁填null不猜" | Expected guard against false filters — mention the exact taxonomy name, or pass filters directly to `/search` |

---

## File import {#import}

| Symptom | Cause | Fix |
|---|---|---|
| A PDF file ends `failed` with a scanned/image-only error (`ScannedPdfError`) | PDF has no text layer and OCR is off | Enable OCR (`KB_INGEST__OCR_ENABLED=true` + the `ocr` extra / `INSTALL_OCR=true` image), or retry that file with `force_ocr`: `POST …/files/{hash}/retry {"force_ocr":true}` |
| File finishes but the UI shows **"No documents extracted"** (0 staged docs) | Text extracted fine, but segmentation returned nothing — usually an over-budget chunk truncated by the LLM, or all entries sat in a skipped chunk | Fixed in `chunk_pages` (overlap is now budget-bounded, every chunk ≤ `segmentation_chunk_chars`) and `_split_oversized_page` (preamble before the first heading is kept). Check the file's `skipped_chunks` for `no_entries`/`parse_failed`; lower `KB_INGEST__SEGMENTATION_CHUNK_CHARS` or set a knowledge-type hint and re-upload |
| File `status: skipped_duplicate` | Its SHA-256 hash was already committed | Intended dedup; re-import with `force=true` on upload/scan if you really mean to |
| File `status: unsupported` | Extension not in `allowed_extensions` | Add it to `KB_INGEST__ALLOWED_EXTENSIONS` (pdf/xlsx/xls/csv/pptx/docx by default) |
| Large file rejected | Over `max_file_size_mb` (50), or PDF over `pdf_max_pages` (2000) / XLSX over `xlsx_max_cells` (2M) | Raise the bound, or split the file — these guard against OOM during extraction |
| `GET /sessions/{id}` returns **410** | Session expired and was swept by the TTL evictor | Re-upload; tune `KB_INGEST__SESSION_TTL_MINUTES` / `SESSION_HARD_TTL_MINUTES` |
| `GET /sessions/{id}` returns **404** | The id never existed (vs 410 = expired) | Check the id; it came from the original upload/scan response |
| Commit returns `tracking_failed > 0` | Docs reached ES but their tracker rows didn't update | Call `POST …/recommit-tracking` — otherwise they're dropped on the next startup reseed |

---

## Persistence & data {#persistence}

| Symptom | Cause | Fix |
|---|---|---|
| Edited a CSV but the change didn't appear | Seeding only happens at startup | Restart the app — `seed()` clears every main index and reloads from the CSVs on each boot |
| Imported docs vanished after a restart | They aren't in the CSVs; restore reads the tracker | Confirm `kb_import_files` is intact; `restore_imports()` replays committed imports at startup. If `tracking_failed` happened at commit, they were never tracked — re-import |
| Documents disappeared after changing the embedding model | A `dims` change recreates indices; the always-reseed then reloads from CSV/tracker | Expected; ensure the tracker survived so imports restore |

---

## Docs site {#docs}

| Symptom | Cause | Fix |
|---|---|---|
| `mkdocs build --strict` fails on a broken link/anchor | A cross-page link or `{#anchor}` doesn't resolve | Fix the link; strict mode also catches the `navigation.instant` ↔ i18n incompatibility |
| `git push` aborted by the `pre-push` hook | The strict docs build failed | Fix the docs, or bypass once with `git push --no-verify` (see [Deployment → Docs auto-publish](deployment.md#docs-publish)) |
| Language switcher 404s when switching zh⇄en | `site_url` missing/wrong for the GitHub Pages subpath | Ensure `site_url` is set in `mkdocs.yml` (the switcher needs it to emit base-prefixed links) |

---

When a fix isn't obvious, grep the logs for the request's `request_id` (every line carries
it) to replay exactly what happened on that call — see
[Observability → Logging](../observability.md#logging).
