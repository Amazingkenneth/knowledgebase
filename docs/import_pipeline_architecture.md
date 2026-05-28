# File Import Pipeline Architecture

## Overview

The import pipeline turns arbitrary office documents (PDF, XLSX/XLS, CSV, PPTX, DOCX) into validated `KnowledgeDoc` entries in Elasticsearch. The pipeline is **review-gated**: the LLM extracts structure, but no document reaches the searchable indices until a human accepts the staged result.

Same zero-fabrication contract as the rest of the system: the LLM only segments and labels — it must copy source text verbatim. Endpoints live under `POST /api/v1/ingest/*` and return HTTP 503 when `KB_LLM__API_KEY` is unset.

- `POST /api/v1/ingest/upload` — multipart upload, returns a session
- `POST /api/v1/ingest/scan` — scan a server-side folder
- `GET  /api/v1/ingest/sessions[/{id}]` — list / inspect
- `PUT  /api/v1/ingest/sessions/{id}/documents/{idx}` — edit staged doc
- `PATCH /api/v1/ingest/sessions/{id}/documents/{idx}` — accept / reject
- `POST /api/v1/ingest/sessions/{id}/commit` — write accepted docs to ES

---

## End-to-End Flow

```
Client (files or folder path + optional hints)
        │
        ▼
[0] Hash & dedupe
        │  SHA-256 of bytes → check kb_import_files index
        │  committed before → SKIPPED_DUPLICATE (unless force=true)
        │  else → record_pending() in tracker, persist file to upload_dir
        │
        ▼
[1] Extraction (per filetype)
        │  PDF: pymupdf text → OCR fallback (PaddleOCR) when page is image-only
        │  XLSX/XLS: openpyxl, one "page" per sheet
        │  CSV: stdlib csv, one "page" per row block
        │  PPTX: python-pptx, one "page" per slide
        │  DOCX: python-docx, paragraphs grouped into pages
        │  → list[(page_number, text)]
        │
        ▼
[2] Knowledge-type detection (skipped if hinted)
        │  LLM sees first 3 pages → returns alarm | setup | experience
        │
        ▼
[3] LLM segmentation
        │  pages chunked by ingest.segmentation_chunk_chars (default 12000)
        │  oversized single pages structurally subdivided (heading/paragraph/line)
        │  1-page overlap between chunks; duplicates collapsed per knowledge type
        │  one LLM call per chunk with a type-specific verbatim-only prompt
        │  on JSON failure: salvage longest valid prefix → repair retry →
        │                   binary-split chunk and recurse (floor: 1 page)
        │  → StagedDocument[] (index, fields, confidence, source_pages, warnings)
        │  on_chunk_progress reports "AI analysis: i/n" to the session
        │
        ▼
[4] Session moves to READY
        │  ImportSession.documents populated; status = ready_for_review
        │
        ▼ (client reviews / edits / accept-rejects)
        │
[5] POST /commit
        │  for each accepted StagedDocument:
        │    → _staged_to_knowledge_doc(): cast to Alarm/Setup/ExperienceDoc
        │    → validate_against_taxonomy()
        │    → embed [title_text, body_text] via DashScope (best-effort)
        │    → ES index into kb_<type>_v1 alias with refresh="wait_for"
        │    → group by file_hash for tracker update
        │  record_committed(file_hash, [es_actions])
        │  → CommitResponse {committed, skipped, errors}
```

Step 1–4 run in a background `asyncio.create_task`; the upload/scan endpoints return `202 Accepted` immediately with the `session_id`. Clients poll `GET /sessions/{id}` (which carries `files_processed`, per-file `status`/`message`, and the human-readable session `message`) until `status == ready_for_review`.

---

## Extraction (`services/extraction.py`)

Each filetype has a dedicated extractor that returns `list[PageText] = list[(int, str)]`. Page numbers are preserved end-to-end so segmented documents carry `source_pages` back to the original.

| Type | Backend | Notes |
|---|---|---|
| PDF | `pymupdf` (fitz) | Prose via `page.get_text` + tables via `page.find_tables()` rendered as pipe-grids; OCR fallback when direct text is short and the page contains images |
| XLSX/XLS | `openpyxl` | One sheet = one page; rows rendered as `\| col \| col \|` pipe-grids; sheet name tagged at the top |
| CSV | stdlib `csv` | Encoding auto-detected (utf-8-sig / utf-8 / gb18030 / latin-1); rows tab-joined |
| PPTX | `python-pptx` | One slide = one page; tables rendered as pipe-grids; speaker notes appended |
| DOCX | `python-docx` | Paragraphs + tables rendered as pipe-grids |

**Table awareness.** For PDF, DOCX, PPTX, and XLSX, tables are rendered as `| cell | cell | cell |` rows so column/row relationships survive into the LLM prompt — both horizontal (header-on-top) and vertical (header-on-left) layouts are preserved as whatever the underlying library returns. The flat-token view from `get_text` is kept alongside the grid view; the LLM sees both. Embedded `|` inside cells is replaced with `/` to keep the grid parseable.

**PDF text cleaning.** `_clean_extracted_text` strips NULs, soft hyphens (`\xad`), BOMs, form feeds, and other stray C0 controls that PDF extractors commonly leak; collapses Windows/Mac line endings; and collapses runs of 3+ blank lines. This means downstream segmentation and ES indexing don't have to defend against invisible characters that would otherwise break search or JSON parsing.

**OCR fallback** runs only when `ingest.ocr_enabled = true` and the direct text is short (or low printable-ratio) on a page that contains images. PaddleOCR (`ocr_lang` defaults to `ch`) is loaded lazily on first use and adds noticeable cold-start latency. The OCR result **replaces** the direct text only when it is meaningfully longer (>20%) **and** passes a printable-character sanity check — this prevents OCR garbage from clobbering good extracted text on pages where both happen to produce output. OCR failures are caught and logged; the direct text wins by default.

Optional dependencies are imported via `_try_import` — a missing optional backend (e.g. PaddleOCR) does not crash the server, but the affected file fails with a clear `ImportError` message. Install the extras with `pip install -e ".[ingest]"`.

---

## Segmentation (`services/segmentation.py`)

The LLM acts as a **structuring parser**, not a writer. Three system prompts (`_ALARM_SYSTEM_PROMPT`, `_SETUP_SYSTEM_PROMPT`, `_EXPERIENCE_SYSTEM_PROMPT`) instruct the model to:

1. Copy source text verbatim — never paraphrase, fabricate, or summarize.
2. Use `"—"` for fields absent from the source rather than inventing content.
3. Treat `| col | col | col |` lines as table rows and preserve cell order.
4. Emit a JSON array of typed segments with a per-entry `confidence` score (0.0–1.0).

### Chunking

`chunk_pages()` packs pages into `segmentation_chunk_chars`-bounded chunks with `_OVERLAP_PAGES = 1` page of overlap so entries spanning a chunk boundary are still seen whole.

Before packing, `_split_oversized_page()` structurally subdivides any single page that exceeds `max_chars`. The split tries, in order:

1. **Heading-like boundaries** — markdown headings, Chinese `第N章/节`, English `Chapter N`, numbered sections (`1.2.3 …`), all-caps lines.
2. **Paragraph breaks** (`\n\n`).
3. **Line breaks** (`\n`).
4. **Hard character cut** (last resort, for a single line larger than `max_chars`).

Sub-pages keep the **original page number**, so `source_pages` traceability is preserved. This closes the silent hole where a single oversized page (e.g. a one-page DOCX, a huge spreadsheet sheet, or a long-form PDF page) used to be fed to the LLM beyond its input budget.

### JSON robustness

The LLM can fail in several ways: truncated output (hit `max_tokens` mid-element), illegal control chars copied from a noisy PDF, leading prose like "Here is the JSON:", or markdown fences. `_parse_json_array()` handles all of these:

1. Strip markdown fences and sanitize C0 control chars.
2. Try a direct `json.loads`.
3. On failure, scan for `[`, then walk bracket/quote depth to find the **longest valid prefix** of the array — recovers complete entries even when the response is truncated mid-element.
4. Promote a bare object to a single-element list.

If parsing still fails for a chunk, `_segment_chunk_with_fallback()` applies two recovery layers:

- **Repair retry** (once per chunk): the LLM is shown its own bad output and asked to re-emit valid JSON.
- **Binary-split recovery**: the failed chunk is split in half on a page boundary and each half is re-segmented (recursion floor: a single page, where giving up cleanly is better than thrashing). This is the answer to the `max_tokens`-exceeded-by-a-single-entry case — the chunk shrinks until the entry fits.

Network/HTTP errors from the LLM also trigger binary-split recovery rather than dropping the chunk.

### Deduplication across overlapping chunks

`_deduplicate_entries()` collapses duplicates produced by the 1-page chunk overlap, with type-specific keys:

| Type | Dedup key | Tie-break |
|---|---|---|
| ALARM | normalized `error_code` | higher `confidence` wins |
| SETUP | normalized `station` + first 80 chars of `procedure` | higher `confidence` wins |
| EXPERIENCE | normalized `problem` + first 80 chars of `failure_desc` | higher `confidence` wins |

Entries with an empty key are **kept as-is** rather than collapsed together — borderline data is surfaced to the human reviewer instead of silently merged.

### Knowledge-type detection

`detect_knowledge_type()`: when the client gives no `knowledge_type_hint`, the LLM is asked to classify the file as `alarm | setup | experience`. The sample stitches **head + middle + tail** page excerpts (`_build_type_detection_sample`, ~800 chars per slot) so that cover pages, TOCs, and front-matter at the start of the file don't dominate the classification. On any failure the type defaults to `experience` (loosest schema). The chosen type routes the file to the appropriate segmentation prompt and ultimately to the matching ES alias (`kb_alarm_v1`, `kb_setup_v1`, `kb_experience_v1`).

**One type per file.** A file is classified once and the entire file is segmented with that one prompt. Documents that genuinely mix knowledge types (e.g. a PDF containing both alarm tables and setup procedures) must be split manually before import.

### Fidelity check (anti-fabrication)

After segmentation, each verbatim-required field (`content`, `resolution`, `procedure`, `failure_desc`) is checked against the source text via `verify_extraction_fidelity()`. The check runs against the **chunk text first** (strict), then falls back to the **full-file text** (catches content that legitimately spans a chunk boundary). On failure the field is kept but the staged document carries a `fabrication_warning: <field>` entry for the reviewer.

### Hints and timeouts

`project_hint` / `equipment_hint` are passed through into the segmentation prompt so detected entries can be pre-populated when the source file does not name them explicitly. The user can still edit these in the preview step.

`_estimate_timeout()` derives the HTTP read-timeout from the actual payload size using a CJK-aware token estimator (CJK ≈ 1.5 tok/char, Latin ≈ 0.25 tok/char). Long chunks won't time out — they'll hit the `max_tokens` ceiling first and recover via the binary-split path.

---

## Session State and Review

```python
class ImportSession:
    session_id: str          # uuid4
    status: ImportStatus     # extracting | ready_for_review | committed | failed
    files: list[FileInfo]    # per-file extraction status
    documents: list[StagedDocument]
    ...hints, created_at
```

Sessions are **in-memory only** (`ImportPipeline._sessions: dict[str, ImportSession]`). A server restart drops all in-flight sessions; the user must re-upload. Already-committed files are unaffected — those live in ES and are restored automatically on next startup (see Tracker).

`StagedDocument` carries all type-specific fields union-style (`content`/`resolution` for alarms, `procedure`/`prerequisites` for setup, `body_text` for experience). `accepted` defaults to `True`; the client toggles it via the PATCH endpoint. Field edits go through PUT and mutate the object directly — there is no diff history.

---

## Commit Path (`commit_session`)

For each `StagedDocument` with `accepted=True`:

1. `_staged_to_knowledge_doc` builds the correct subclass (`AlarmDoc` / `SetupDoc` / `ExperienceDoc`) from the staged fields. Missing required strings default to `"—"`; a missing setup title falls back to `f"{equipment} 调试"`.
2. `validate_against_taxonomy` rejects unknown `project` / `equipment` values — these surface as `string_too_short` / validation errors and are aggregated into the `errors` array.
3. `EmbeddingClient.embed([title_text, body_text])` runs **best-effort**: any failure logs a warning and the document is indexed with `null` vectors (BM25 still works; vector rescore silently drops it).
4. `es.index(...)` with `refresh="wait_for"` writes into the type-appropriate alias, keyed by `doc_id(doc)` — a stable hash so re-commits are idempotent.
5. The ES action (`{_index, _id, _source}`) is collected per source-file `file_hash`.

After the loop, `record_committed(file_hash, actions)` updates the tracker. Validation/indexing failures **break** the loop for that doc but the loop intentionally `break`s on the first error so the user can fix and re-commit without partial state surprises.

---

## File Tracker (`kb_import_files` index)

The tracker has two jobs: **dedupe** and **auto-restore**.

**Dedupe**: keyed by SHA-256 of file bytes. `start_upload` checks `tracker.exists(hash)` before persisting; if the prior import is `committed` and the user did not pass `force=true`, the file is marked `SKIPPED_DUPLICATE`.

**Auto-restore**: each committed doc's full ES source is stored under `committed_docs[]` on the tracker record. On startup, `seed` clears the main indices from CSV; then `restore_imports()` (in `services/seed.py`) calls `tracker.get_all_committed()` and bulk re-indexes every payload back into the appropriate alias. This is why imported documents survive the always-reseed-on-startup behavior — the tracker, not the source files, is the source of truth for imports.

Lifecycle states stored on the tracker record:

| `import_status` | Set by | Meaning |
|---|---|---|
| `pending` | `record_pending()` at upload time | File persisted, awaiting extraction |
| `committed` | `record_committed()` after commit | All accepted docs indexed; payloads cached for restore |
| `failed` | `record_failed()` on extraction error | Error message stored; will not auto-restore |

---

## Configuration

All knobs live under `ingest:` in `config/settings.yaml` or as `KB_INGEST__*` env vars.

| Key | Default | Effect |
|---|---|---|
| `ingest.upload_dir` | `data/uploads` | Where uploaded files are persisted (`<hash>_<name>`) |
| `ingest.max_file_size_mb` | `50` | Per-file size cap; oversize → FAILED with message |
| `ingest.allowed_extensions` | `pdf, xlsx, xls, csv, pptx, docx` | Anything else → UNSUPPORTED |
| `ingest.ocr_enabled` | `true` | When false, PDF pages with little direct text yield empty |
| `ingest.ocr_lang` | `ch` | PaddleOCR language pack |
| `ingest.segmentation_max_tokens` | `8000` | LLM max-tokens for segmentation calls |
| `ingest.segmentation_chunk_chars` | `12000` | Characters per chunk fed to the segmenter |
| `ingest.session_ttl_minutes` | `120` | (Reserved) intended session retention |

---

## Key Design Constraints

- **Review-gated**: nothing reaches the search indices without an explicit commit step. Even the "fast path" (scan an entire folder) ends at `ready_for_review`.
- **Verbatim only**: segmentation prompts forbid paraphrase. Confidence scores on each segment let reviewers triage borderline entries; low-confidence docs still arrive in the preview but warrant inspection.
- **Best-effort embedding**: embedding errors during commit never abort indexing — the doc lands without vectors and remains BM25-searchable.
- **Dedupe by content hash**: filename is irrelevant; the same bytes uploaded twice short-circuit unless `force=true`.
- **Imports survive CSV re-seed**: the always-reseed-on-startup behavior wipes the main indices; the tracker's `committed_docs` cache is replayed afterwards so imports persist across restarts.
- **In-memory sessions**: a server restart loses any session not yet committed. This is an intentional simplification — re-extraction is cheap relative to disk-persisting partial state.
