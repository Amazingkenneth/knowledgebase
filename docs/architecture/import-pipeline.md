# File Import Pipeline Architecture

The import pipeline turns arbitrary office documents (PDF, XLSX/XLS, CSV, PPTX,
DOCX) into validated `KnowledgeDoc` entries in Elasticsearch. The pipeline is
**review-gated**: the LLM extracts structure, but no document reaches the
searchable indices until a human accepts the staged result.

Same zero-fabrication contract as the rest of the system: the LLM only segments and
labels — it must copy source text verbatim. Endpoints live under
`POST /api/v1/ingest/*` and return **HTTP 503** when `KB_LLM__API_KEY` is unset.

- `POST /api/v1/ingest/upload` — multipart upload, returns a session
- `POST /api/v1/ingest/scan` — scan a server-side folder
- `GET  /api/v1/ingest/sessions[/{id}]` — list / inspect
- `PUT  /api/v1/ingest/sessions/{id}/documents/{idx}` — edit staged doc
- `PATCH /api/v1/ingest/sessions/{id}/documents/{idx}` — accept / reject
- `POST /api/v1/ingest/sessions/{id}/commit` — write accepted docs to ES

Orchestration lives in `src/kb/services/import_pipeline.py`.

---

## End-to-end flow

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
[2] Per-chunk routing (skipped when knowledge_type_hint locks the file)
        │  pages chunked by ingest.segmentation_chunk_chars (default 12000)
        │  for each chunk → LLM router returns the type list (one dominant by default):
        │      {"types": ["alarm"]}                 ← single dominant type (default)
        │      {"types": ["alarm", "setup"]}        ← only when both types stand
        │                                              alone with distinct structure
        │      {"types": ["skip"]}                  ← non-content (cover/TOC/preface)
        │  skip → drop the chunk with a friendly SkippedChunk (reason, hint)
        │
        ▼
[3] LLM segmentation (one call per detected type per chunk)
        │  prompts rendered from config/knowledge_types/<type>.yaml — single
        │    source of truth for the LLM contract AND the pydantic model
        │  each per-type call carries an "ignore other-type content" rule and a
        │    "return [] — never emit empty skeletons" rule so mixed chunks and
        │    router false-positives don't pollute the output
        │  oversized single pages structurally subdivided (heading/paragraph/line)
        │  1-page overlap; duplicates collapsed per knowledge type
        │  on JSON failure: salvage longest valid prefix → object sweep →
        │                   repair retry → binary-split chunk and recurse (floor: 1 page)
        │  entry validation: drop entries with empty required fields or
        │                   confidence < 0.3 (router false-positive guard)
        │  project/equipment: LLM-extracted verbatim > filename/upload hint > 所有项目
        │  → (StagedDocument[], SkippedChunk[])
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

Steps 1–4 run in a background `asyncio.create_task`; the upload/scan endpoints
return `202 Accepted` immediately with the `session_id`. Clients poll
`GET /sessions/{id}` (which carries `files_processed`, per-file `status`/`message`,
and the human-readable session `message`) until `status == ready_for_review`.

---

## Extraction (`services/extraction.py`)

Each filetype has a dedicated extractor that returns `list[PageText] =
list[(int, str)]`. Page numbers are preserved end-to-end so segmented documents
carry `source_pages` back to the original.

| Type | Backend | Notes |
|---|---|---|
| PDF | `pymupdf` (fitz) | Prose via `page.get_text` + tables via `page.find_tables()` rendered as pipe-grids; OCR fallback when direct text is short and the page contains images |
| XLSX/XLS | `openpyxl` | One sheet = one page; rows rendered as `\| col \| col \|` pipe-grids; sheet name tagged at the top |
| CSV | stdlib `csv` | Encoding auto-detected (utf-8-sig / utf-8 / gb18030 / latin-1); rows tab-joined |
| PPTX | `python-pptx` | One slide = one page; tables rendered as pipe-grids; speaker notes appended |
| DOCX | `python-docx` | Body walked in **document order** (`body.iterchildren()`), so paragraphs and tables stay interleaved as written; tables rendered as pipe-grids |

**Table awareness.** For PDF, DOCX, PPTX, and XLSX, tables are rendered as
`| cell | cell | cell |` rows so column/row relationships survive into the LLM
prompt — both horizontal (header-on-top) and vertical (header-on-left) layouts are
preserved as whatever the underlying library returns. The flat-token view from
`get_text` is kept alongside the grid view; the LLM sees both. Embedded `|` inside
cells is replaced with `/` to keep the grid parseable.

**DOCX document order.** DOCX extraction walks the document body in true reading
order (`doc.element.body.iterchildren()`, dispatching on the `w:p` / `w:tbl` tags)
rather than the old "all paragraphs, then all tables" two-pass. The two-pass
destroyed locality between an entry written in prose and a following one-row table
that named its Project / Equipment — interleaving them keeps that relationship
intact for the segmenter and for the verbatim project/equipment extraction below.

**PDF text cleaning.** `_clean_extracted_text` strips NULs, soft hyphens (`\xad`),
BOMs, form feeds, and other stray C0 controls that PDF extractors commonly leak;
collapses Windows/Mac line endings; and collapses runs of 3+ blank lines. So
downstream segmentation and ES indexing don't have to defend against invisible
characters that would otherwise break search or JSON parsing.

**OCR fallback** runs only when `ingest.ocr_enabled = true` and the direct text is
short (or low printable-ratio) on a page that contains images. PaddleOCR
(`ocr_lang` defaults to `ch`) is loaded lazily on first use and adds noticeable
cold-start latency. The OCR result **replaces** the direct text only when it is
meaningfully longer (>20%) **and** passes a printable-character sanity check — this
prevents OCR garbage from clobbering good extracted text on pages where both happen
to produce output. OCR failures are caught and logged; the direct text wins by
default.

Optional dependencies are imported via `_try_import` — a missing optional backend
(e.g. PaddleOCR) does not crash the server, but the affected file fails with a clear
`ImportError` message. Install the extras with `pip install -e ".[ingest]"`.

---

## Knowledge-type specs (`config/knowledge_types/*.yaml`)

Every knowledge type has a single spec file that drives both the LLM prompt and the
storage contract. Editing the YAML changes what the LLM is told to extract *and*
what the parity test enforces against the pydantic model — they cannot drift.

```
config/knowledge_types/
├── alarm.yaml        ← mirrors config/机台报警_header.csv
├── setup.yaml        ← mirrors config/机台setup_header.csv
└── experience.yaml   ← mirrors config/设备经验_header.csv
```

Each spec carries:

| Block | Purpose |
|---|---|
| `summary_zh` / `summary_en` | One-liner shown in the router prompt so the LLM knows when to pick this type |
| `fields[]` | Output JSON shape — each field has `name`, `desc`, optional `label_zh`, `csv_column`, and `required` flag. All three specs include `project` and `equipment` fields the LLM fills **verbatim from the source** ("Project: X" / "项目: X" cells), or `""` when not stated — it must never guess them from the filename or context |
| `boundary_hints[]` | What to look for when splitting entries |
| `skip_if[]` | Patterns that mean "this isn't content" (cover, TOC, preface…) |
| `confidence_guide` | Rubric for the per-entry `confidence` score |
| `example_input` / `example_output` | Worked few-shot example drawn from the canonical CSV row |

`services/spec.py` loads and caches the YAMLs, then renders two prompts:

- **`render_segmentation_prompt(spec)`** — the per-type extractor prompt. Includes
  the field list (with zh labels and CSV-column links shown to the LLM), the worked
  example, and several explicit rules: (a) *"ONLY extract `<type>` entries. If the
  chunk also contains other knowledge-type content, IGNORE it"* — lets a single
  chunk be parsed safely by both the alarm and setup extractors without
  cross-pollination; (b) *"if the chunk contains NO `<type>` entries, return an
  empty array `[]`. Do NOT emit skeleton entries with empty required fields just to
  fill the array"*; (c) *"required fields (`<computed from spec>`) MUST be populated
  verbatim for every emitted entry — if you can't find a required field's value,
  DROP the entry."* The required-field list in (b)/(c) is computed from the spec's
  `required: true` fields, so the prompt always matches what entry validation
  enforces.
- **`render_router_prompt(specs)`** — the classifier prompt. Returns
  `{"types": [...]}` (a list), but the rules bias toward **exactly one dominant
  type**: multiple types are returned only when entries of each type stand alone
  with their own distinct structure (e.g. a standalone alarm-code table *and* a
  separate numbered tuning procedure). The prompt carries negative examples — an
  alarm's "Remedy / 解除流程" numbered steps are still `["alarm"]` (not setup), and a
  setup procedure that references alarm codes inline is still `["setup"]`. This
  curbs the over-fanout that produced router false-positives.

A parity test (`tests/unit/test_spec.py`) asserts that every required pydantic field
is covered by a spec field, and that the spec's `example_output` round-trips through
`_parsed_to_staged()` without losing any required content — drift between prompt and
model is caught at test time, not at commit time.

---

## Segmentation (`services/segmentation.py`)

The LLM acts as a **structuring parser**, not a writer. Per-type system prompts are
rendered from the spec YAMLs above and instruct the model to:

1. Copy source text verbatim — never paraphrase, fabricate, or summarize.
2. Use `""` for *optional* fields absent from the source. Required content fields
   must be filled verbatim, or the entry is dropped — do not invent placeholders.
3. Treat `| col | col | col |` lines as table rows and preserve cell order.
4. Emit a JSON array of typed segments with a per-entry `confidence` score
   (0.0–1.0). When the chunk contains no entries of the target type, emit an empty
   array `[]` — never a skeleton entry with blank fields.
5. Extract ONLY the prompt's target type; ignore other-type content in the same
   chunk.

> The robustness pipeline is layered outer → inner: **structural chunking → JSON
> salvage (longest-prefix + object sweep) → repair retry → binary-split recovery →
> entry validation → cross-chunk dedup.** Each layer is detailed below.

### Chunking

`chunk_pages()` packs pages into `segmentation_chunk_chars`-bounded chunks with
`_OVERLAP_PAGES = 1` page of overlap so entries spanning a chunk boundary are still
seen whole. The overlap is **budget-aware**: the previous page is carried into the
next chunk only when it still leaves room for the incoming page
(`len(overlap) + len(page) <= max_chars`); otherwise the next chunk starts fresh.
This guarantees **every chunk stays within `max_chars`** — re-adding a near-full
page as overlap used to combine with the next page into an over-budget chunk (e.g.
16k against a 12k budget), which the LLM could truncate mid-JSON and silently drop
every entry on the page ("No documents extracted").

Before packing, `_split_oversized_page()` structurally subdivides any single page
that exceeds `max_chars`. The split tries, in order:

1. **Heading-like boundaries** — markdown headings, Chinese `第N章/节`, English
   `Chapter N`, numbered sections (`1.2.3 …`), all-caps lines.
2. **Paragraph breaks** (`\n\n`).
3. **Line breaks** (`\n`).
4. **Hard character cut** (last resort, for a single line larger than `max_chars`).

When splitting on headings, any **preamble before the first heading** is kept as
its own segment rather than discarded — otherwise entries that sit above the first
heading line (e.g. table-rendered alarm rows that precede the first plain-text
heading) would vanish, which can leave a file with zero extracted documents.

Sub-pages keep the **original page number**, so `source_pages` traceability is
preserved. This closes the silent hole where a single oversized page (e.g. a
one-page DOCX, a huge spreadsheet sheet, or a long-form PDF page) used to be fed to
the LLM beyond its input budget.

### JSON robustness

The LLM can fail in several ways: truncated output (hit `max_tokens` mid-element),
illegal control chars copied from a noisy PDF, leading prose like "Here is the
JSON:", or markdown fences. `_parse_json_array()` handles all of these:

1. Strip markdown fences and sanitize control chars (see below).
2. Try a direct `json.loads`.
3. On failure, scan for `[`, then walk bracket/quote depth to find the **longest
   valid prefix** of the array — recovers complete entries even when the response is
   truncated mid-element.
4. Promote a bare object to a single-element list.
5. **Object sweep** (last-ditch): `_sweep_json_objects()` walks the whole text
   collecting every balanced `{…}` substring that parses as a dict on its own —
   ignoring commas and array syntax entirely. This catches responses shaped like
   `[ prose… {…}{…} ]` where the outer-array salvage gives up because nothing legal
   sits between the objects, the failure mode seen when a per-type segmenter is
   asked to extract from a chunk with no entries and improvises.

**Control-char sanitization** (`_sanitize_json`) is string-aware rather than a
blanket strip. Per RFC 8259, raw TAB/LF/CR are illegal *inside* JSON string
literals, and models like Qwen-turbo routinely emit them inside `content` /
`resolution` strings — the dominant root cause of the old `Unrecoverable JSON: char
0` failures. The sanitizer tracks string vs. non-string context: **inside** a string
it escapes TAB/LF/CR to `\t`/`\n`/`\r` and drops other C0 controls; **outside** a
string it leaves whitespace untouched.

If parsing still fails for a chunk, `_segment_chunk_with_fallback()` applies two
recovery layers:

- **Repair retry** (once per chunk): the LLM is shown its own bad output and asked
  to re-emit valid JSON (the repair prompt also says "if there are no entries,
  return `[]`"). `_try_repair_json()` returns `None` — distinct from an empty list —
  when the repair call or its parse also fails, signalling the caller to fall
  through to binary-split rather than treating a failure as "no entries".
- **Binary-split recovery**: the failed chunk is split in half on a page boundary
  and each half is re-segmented (recursion floor: a single page). This is the answer
  to the `max_tokens`-exceeded-by-a-single-entry case — the chunk shrinks until the
  entry fits.

Network/HTTP errors from the LLM also trigger binary-split recovery rather than
dropping the chunk. A chunk which **parses** but yields only skeleton/low-confidence
entries is *not* a parse failure — it is filtered by entry validation (below) and
returns empty without retrying or splitting.

### Entry validation (router false-positive guard)

`_filter_valid_entries()` runs on every parsed (and repaired) entry list before the
entries are accepted. An entry is **dropped** when:

- its `confidence` is below `_HARD_DROP_CONFIDENCE = 0.3`, **or**
- any field marked `required: true` in the spec is empty (value in
  `{"", "—", "-", "n/a", "na", "none", "null"}`, or an empty list/dict).

This is the primary defense against the **router-false-positive** failure mode: when
the classifier fans a chunk out to a type it doesn't actually contain, the per-type
segmenter is asked to extract entries that aren't there and the LLM tends to emit
skeleton dicts with blank fields and `confidence: 0.0` instead of an empty array.
Dropping them upstream means a routed type that returns nothing is treated as a
genuine no-entry, not surfaced as a low-confidence document for the reviewer to wade
through.

### Deduplication across overlapping chunks

`_deduplicate_entries()` collapses duplicates produced by the 1-page chunk overlap,
with type-specific keys:

| Type | Dedup key | Tie-break |
|---|---|---|
| ALARM | normalized `error_code` | higher `confidence` wins |
| SETUP | normalized `station` + first 80 chars of `procedure` | higher `confidence` wins |
| EXPERIENCE | normalized `problem` + first 80 chars of `failure_desc` | higher `confidence` wins |

Entries with an empty key are **kept as-is** rather than collapsed together —
borderline data is surfaced to the human reviewer instead of silently merged.

### Per-chunk multi-type routing

`classify_chunk_types()` classifies each chunk independently and returns the
**list** of knowledge types present:

- `[]` (router said `skip`) → drop the chunk; surface a
  `SkippedChunk(reason="non_content")` with a friendly hint.
- `[KnowledgeType.ALARM]` → one segmenter call, alarm prompt. **This is the
  default** — the router prompt asks for exactly one dominant type.
- `[KnowledgeType.ALARM, KnowledgeType.SETUP]` → two segmenter calls on the **same
  chunk text**; each parser extracts only its own entries because the prompt
  explicitly tells it to ignore other-type content. The router only fans out like
  this when entries of each type stand alone with their own distinct structure.

When the client passes `knowledge_type_hint` on upload, the hint **locks** every
chunk to that type and the router is skipped entirely — use this when you know the
whole file is one type and don't want to pay the classifier cost.
`detect_knowledge_type()` is retained as a thin wrapper returning the first entry
from `classify_chunk_types`.

### Non-content handling (covers, TOCs, prefaces)

Pages that aren't content (cover, table of contents, preface, revision history,
glossary, index, copyright notice, or pure prose) are detected by the router via
each spec's `skip_if[]` rules and dropped before segmentation. They surface to the
UI as `FileInfo.skipped_chunks: list[SkippedChunk]`, each carrying:

- `page_range` — which pages were skipped
- `reason` — `non_content` | `no_entries`
- `hint` — a plain-language explanation the reviewer can act on

A whole-chunk `no_entries` hint is only raised when **every** routed type came back
empty. The file-card `message` summarizes the counts: *"Extracted 14 documents. 2
non-content page(s) skipped (covers/TOC/preface)."*

### Fidelity check (anti-fabrication)

After segmentation, each verbatim-required field (`content`, `resolution`,
`procedure`, `failure_desc`) is checked against the source text via
`verify_extraction_fidelity()`. The check runs against the **chunk text first**
(strict), then falls back to the **full-file text** (catches content that
legitimately spans a chunk boundary). On failure the field is kept but the staged
document carries a `fabrication_warning: <field>` entry for the reviewer.

### Project & equipment resolution

`project` / `equipment` are resolved through a three-tier priority chain so every
staged doc lands with a usable, taxonomy-valid project without ever blocking the
reviewer:

1. **LLM-extracted, verbatim** — each spec declares `project` / `equipment` fields.
   The segmenter fills them only when the source explicitly names them; otherwise
   `""`. `_parsed_to_staged()` reads `entry["project"]` / `entry["equipment"]` and
   prefers them over the upload hint.
2. **Filename / upload hint** — when the entry carries no value, the upload-time
   `project_hint` / `equipment_hint` is used. If those weren't supplied,
   `_detect_taxonomy_from_filename()` tokenizes the filename stem and matches a
   taxonomy value only as a **whole lowercase token** — so `PDX-aligner-faults.pdf`
   → `project=PDX, equipment=Aligner`, but `stages.pdf` won't match a `Stage`
   equipment.
3. **Taxonomy resolution** — after segmentation, `_resolve_taxonomy_fields()`
   validates each doc's `project` / `equipment` against `taxonomy.yaml`
   (case-insensitive, stored in canonical casing). Unknown values are **cleared**
   with a reviewer-visible warning (`unknown_project:` / `unknown_equipment:`).
   Project then falls back to the cross-project bucket `所有项目`; equipment is left
   empty when unknown (it is optional).

The reviewer can override any of these per-doc in the preview UI. The "missing
required field" warning flags only docs with an empty **project**.

### Timeouts

`_estimate_timeout()` derives the HTTP read-timeout from the actual payload size
using a CJK-aware token estimator (CJK ≈ 1.5 tok/char, Latin ≈ 0.25 tok/char). Long
chunks won't time out — they'll hit the `max_tokens` ceiling first and recover via
the binary-split path.

---

## Session state and review

```python
class ImportSession:
    session_id: str          # uuid4
    status: ImportStatus     # extracting | ready_for_review | committed | failed
    files: list[FileInfo]    # per-file extraction status
    documents: list[StagedDocument]
    ...hints, created_at
```

Sessions are **in-memory only** (`ImportPipeline._sessions: dict[str,
ImportSession]`). A server restart drops all in-flight sessions; the user must
re-upload. Already-committed files are unaffected — those live in ES and are
restored automatically on next startup (see [File Tracker](#file-tracker-kb_import_files)).

`StagedDocument` carries all type-specific fields union-style (`content`/`resolution`
for alarms, `procedure`/`prerequisites` for setup, `body_text` for experience).
`accepted` defaults to `True`; the client toggles it via the PATCH endpoint. Field
edits go through PUT and mutate the object directly — there is no diff history.

Idle sessions are reclaimed by a background sweeper: a soft TTL
(`session_ttl_minutes`, default 120) evicts COMMITTED/FAILED sessions, and a hard
TTL (`session_hard_ttl_minutes`, default 480) evicts *any* session including
under-review ones, bounding memory against abandoned previews.

---

## Commit path (`commit_session`)

For each `StagedDocument` with `accepted=True`:

1. `_staged_to_knowledge_doc` builds the correct subclass (`AlarmDoc` / `SetupDoc` /
   `ExperienceDoc`) from the staged fields. Missing required strings default to
   `"—"`; a missing setup title falls back to `f"{equipment} 调试"`.
2. `validate_against_taxonomy` rejects unknown `project` / `equipment` values —
   these surface as validation errors aggregated into the `errors` array.
3. `EmbeddingClient.embed([title_text, body_text])` runs **best-effort**: any
   failure logs a warning and the document is indexed with `null` vectors (BM25
   still works; vector rescore silently drops it).
4. `es.index(...)` with `refresh="wait_for"` writes into the type-appropriate alias,
   keyed by `doc_id(doc)` — a stable hash so re-commits are idempotent.
5. The ES action is collected per source-file `file_hash`.

After the loop, `record_committed(file_hash, actions)` updates the tracker.
Validation/indexing failures **break** the loop for that doc so the user can fix and
re-commit without partial-state surprises.

**Friendly commit errors.** `_friendly_validation_message()` converts raw pydantic
errors into one-line hints keyed to the offending field, e.g. *"'resolution' is
empty. Required for alarms — paste the Remedy / 解除流程 section."* Each entry in
`CommitResponse.errors[]` carries both `error` (the message) and `hint` (what to do
about it).

---

## File tracker (`kb_import_files` index) {#file-tracker-kb_import_files}

The tracker has two jobs: **dedupe** and **auto-restore**.

**Dedupe:** keyed by SHA-256 of file bytes. `start_upload` checks
`tracker.exists(hash)` before persisting; if the prior import is `committed` and the
user did not pass `force=true`, the file is marked `SKIPPED_DUPLICATE`. Failed files
can be reprocessed via the retry endpoint (optionally forcing OCR).

**Auto-restore:** each committed doc's full ES source is stored under
`committed_docs[]` on the tracker record. On startup, `seed` clears the main indices
from CSV; then `restore_imports()` (in `services/seed.py`) calls
`tracker.get_all_committed()` and bulk re-indexes every payload back into the
appropriate alias. This is why imported documents survive the always-reseed-on-
startup behavior — the tracker, not the source files, is the source of truth for
imports.

Lifecycle states stored on the tracker record:

| `import_status` | Set by | Meaning |
|---|---|---|
| `pending` | `record_pending()` at upload time | File persisted, awaiting extraction |
| `committed` | `record_committed()` after commit | All accepted docs indexed; payloads cached for restore |
| `failed` | `record_failed()` on extraction error | Error message stored; will not auto-restore |

---

## Configuration

All knobs live under `ingest:` in `config/settings.yaml` or as `KB_INGEST__*` env
vars — see [Configuration → Ingest](../configuration.md#ingest).

---

## Key design constraints

- **Review-gated** — nothing reaches the search indices without an explicit commit
  step. Even the "fast path" (scan an entire folder) ends at `ready_for_review`.
- **Spec-driven** — each knowledge type is defined once in
  `config/knowledge_types/<type>.yaml`. The LLM prompt, the worked example, the skip
  rules, and the parity check all read from that file.
- **Mixed-type files supported** — routing is per chunk, not per file. The router
  biases toward a single dominant type and only fans out when each type stands alone
  structurally.
- **Verbatim only** — segmentation prompts forbid paraphrase. Entries below the 0.3
  confidence floor or with empty required fields are dropped outright.
- **Project/equipment never block commit** — values are taken verbatim, else
  inferred from the filename, else defaulted to `所有项目`. Equipment is optional.
- **Friendly feedback** — skipped chunks and commit errors carry an actionable
  `hint` rather than a raw stack trace.
- **Best-effort embedding** — embedding errors during commit never abort indexing.
- **Dedupe by content hash** — the same bytes uploaded twice short-circuit unless
  `force=true`.
- **Imports survive CSV re-seed** — the tracker's `committed_docs` cache is replayed
  after the startup reseed.
- **In-memory sessions** — a server restart loses any session not yet committed.
