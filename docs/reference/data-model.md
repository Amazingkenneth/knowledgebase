# Data Model & ES Mapping Reference {#data-model}

This is the **replication-grade** data contract reference: every document field,
Pydantic validation rule, Elasticsearch mapping, `body` text layout, document-ID
algorithm, auxiliary index, API request/response model, and the CSV / knowledge-type
spec field mappings. Implement to this page and you get an index structure that is
byte-for-byte compatible with the project.

Source locations:

| Concern | File |
|---|---|
| Document models (`AlarmDoc` / `SetupDoc` / `ExperienceDoc`) | `src/kb/models/document.py` |
| Search request/response models | `src/kb/models/search.py` |
| Import pipeline models | `src/kb/models/ingest.py` |
| Taxonomy model | `src/kb/models/taxonomy.py` |
| Main index mappings | `src/kb/es/mappings.py` |
| `body` text assembly | `src/kb/es/body_builder.py` |
| Document ID + index-time validation | `src/kb/services/indexing.py` |
| Import-tracker index mapping | `src/kb/es/import_mappings.py` |
| Search-feedback index mapping | `src/kb/es/feedback_mappings.py` |
| Knowledge-type specs (segmentation prompts) | `src/kb/services/spec.py`, `config/knowledge_types/*.yaml` |
| CSV → document mapping | `src/kb/services/csv_loader.py` |

---

## Knowledge type enum {#knowledge-type}

`KnowledgeType` (a `StrEnum`) is the root enum of the whole system — it decides which
index a document lands in, which subclass validates it, and which segmentation prompt
applies:

| Value | Document subclass | Index alias |
|---|---|---|
| `alarm` | `AlarmDoc` | `kb_alarm` |
| `setup` | `SetupDoc` | `kb_setup` |
| `experience` | `ExperienceDoc` | `kb_experience` |

Adding a knowledge type requires touching all of: the `KnowledgeType` enum, a new
`DocumentBase` subclass, the `doc_class_for()` `match`, a
`config/knowledge_types/<type>.yaml` spec, and the `knowledge_types` list in
`config/taxonomy.yaml`.

---

## Scalar type aliases {#scalar-aliases}

The top of `src/kb/models/document.py` defines constrained string aliases reused by
every document field:

| Alias | Definition | Constraint |
|---|---|---|
| `NonEmptyStr` | `Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]` | ≥1 char after stripping |
| `TitleStr` | `Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]` | title: 1–200 chars |
| `SummaryStr` | `Annotated[str, StringConstraints(max_length=50, strip_whitespace=True)]` | summary: ≤50 chars |

Error-code validation regex (used by `error_codes`):

```python
ERROR_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_\-]{0,63}$")
```

First char is an uppercase letter or digit, then uppercase letters / digits /
underscores / hyphens, total length 1–64. The validator `strip().upper()`s each code,
then enforces uniqueness; any non-match raises `ValueError`.

---

## Common document fields — `DocumentBase` {#document-base}

The base class for all knowledge documents. Fields fall into three groups that map
one-to-one onto the ES mapping (see [Index mappings](#es-mappings)):

| Field | Type | Default | Group | Notes |
|---|---|---|---|---|
| `knowledge_type` | `KnowledgeType` | — (locked by subclass) | Part 1 | document type |
| `project` | `str` | `""` | Part 1 | validated against taxonomy at index time |
| `equipment` | `str` | `""` | Part 1 | validated against taxonomy at index time |
| `error_codes` | `list[str]` | `[]` | Part 1 | each matched against `ERROR_CODE_RE`, upper-cased, de-duplicated |
| `title` | `TitleStr` | required | Part 3 | participates in BM25 + vector recall |
| `source_file` | `str \| None` | `None` | Part 2 | source filename (display-only, not indexed) |
| `source_pages` | `list[str]` | `[]` | Part 2 | source page numbers (display-only) |
| `summary` | `SummaryStr \| None` | `None` | Part 2 | ≤50-char digest for result lists / LLM context budget |
| `created_at` | `datetime \| None` | `None` | audit | |
| `updated_at` | `datetime \| None` | `None` | audit | |

!!! note "Retrieval semantics of the three groups"
    - **Part 1 (keyword)** — exact filtering (`term`/`terms`), not tokenized, no BM25 impact.
    - **Part 2 (display-only)** — `index: False` / `enabled: False`, returned verbatim
      for rendering, never queried or scored.
    - **Part 3 (full-text)** — tokenized by the configured analyzer, drives BM25 keyword
      recall and vector rescoring.

Every subclass implements `content_sections() -> list[tuple[str, str]]`, returning
ordered `(section_name, text)` pairs. `body_builder` uses these to assemble `body` and
`sections`; the section name itself does **not** appear in the `body` text (it only
lets the builder skip empty sections).

---

## The three document subclasses {#document-subclasses}

### `AlarmDoc`

| Field | Type | Default | Notes |
|---|---|---|---|
| `content` | `NonEmptyStr` | required | alarm content / trigger conditions, verbatim |
| `resolution` | `NonEmptyStr` | required | remedy steps, verbatim |
| `notes` | `str` | `""` | optional |

`content_sections()` order: `content` → `resolution` → `notes` (if non-empty).

### `SetupDoc`

| Field | Type | Default | Notes |
|---|---|---|---|
| `procedure` | `NonEmptyStr` | required | setup/tuning steps, verbatim |
| `prerequisites` | `str` | `""` | specs / requirements / tools |
| `notes` | `str` | `""` | optional |

`content_sections()` order: `prerequisites` (if non-empty) → `procedure` → `notes` (if non-empty).

### `ExperienceDoc`

| Field | Type | Default | Notes |
|---|---|---|---|
| `body_text` | `NonEmptyStr` | required | failure description (with analysis/root-cause concatenated), verbatim |
| `procedure` | `str` | `""` | corrective steps |
| `notes` | `str` | `""` | optional |

`content_sections()` order: `body` → `procedure` (if non-empty) → `notes` (if non-empty).

!!! tip "Discriminated union"
    `KnowledgeDoc = AlarmDoc | SetupDoc | ExperienceDoc`. Each subclass's
    `knowledge_type` is annotated `Literal[...]` locked to its own type, so once
    `_parse_doc()` injects `knowledge_type` only the matching subclass validates.
    `doc_class_for(kt)` maps the enum back to the subclass with `match`.

---

## `body` text layout {#body-builder}

`build_body(doc)` (`src/kb/es/body_builder.py`) assembles the text indexed into the ES
`body` field in a fixed layout. **The layout is pinned by tests — changing it requires
bumping the index version and reindexing.**

Separator constants:

```python
CONTENT_SEPARATOR = "\n\n---\n\n"   # between sections and the metadata block
META_SEPARATOR    = "\n"             # inside the metadata block
```

Layout (alarm with notes shown):

```
<content text>

---

<resolution text>

---

<notes text>

---

<title>
project: <project>
equipment: <equipment>
error_codes: <code1> <code2> ...
```

Key points:

- Sections are concatenated in `content_sections()` order, joined by `CONTENT_SEPARATOR`.
- A final **metadata block** is appended: first line is `title`, then `project: …`,
  `equipment: …`, and `error_codes: c1 c2` (space-separated) if any codes exist. The
  block is joined by `META_SEPARATOR`.
- Metadata is folded into `body` so a keyword query (e.g. "MHK") recalls a doc even when
  the term lives only in the `project` field — the analyzer splits `project:` from `MHK`.

`build_title_text(doc)` currently returns `doc.title` verbatim (kept for symmetry with
`build_body`, as a seam for prepending project/equipment to the indexed title later).

`title` and `body` each produce a vector — `title_vec` and `body_vec` (see
[Index mappings](#es-mappings)). Current retrieval only rescores / kNNs on `body_vec`.

---

## Document ID generation {#doc-id}

`doc_id(doc)` (`src/kb/services/indexing.py`) produces a **content-addressed**, stable ID
so the same logical document upserts idempotently (editing notes/content updates the
existing doc rather than creating a duplicate):

```python
payload = "|".join([
    doc.knowledge_type.value,
    doc.project,
    doc.equipment,
    doc.title.strip(),
    ",".join(sorted(doc.error_codes)),
])
h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
doc_id = f"{doc.knowledge_type.value}:{h}"   # e.g. alarm:9f2c… (24 hex chars)
```

So ID = `<type>:<sha256(type|project|equipment|title|sorted_codes)[:24]>`. Changing any
of `project`/`equipment`/`title`/`error_codes` yields a new ID (a new document).

---

## Index-time taxonomy validation {#taxonomy-validation}

`validate_against_taxonomy(doc, tax)` runs before embedding and writing, raising
`IndexingError` on violation:

1. `doc.knowledge_type` must be in `tax.knowledge_types`;
2. if `doc.project` is non-empty, it must be in `tax.projects`;
3. if `doc.equipment` is non-empty, it must be in `tax.equipment`.

Empty project/equipment are allowed (treated as "unspecified"), but non-empty unknown
values are rejected — this is where "taxonomy enforcement" lands.

---

## Main index mappings {#es-mappings}

All three knowledge types share one mapping (`_base_mapping`); the only difference is at
the application layer (which sections go into `body`). Sharing one mapping makes
cross-type search trivial. Every mapping is `dynamic: "strict"` — unknown fields are
rejected at write time.

`index_body(dims, index_analyzer, query_analyzer)` full create-index body:

```json
{
  "settings": {
    "analysis": {
      "analyzer": {
        "kb_index": {"type": "custom", "tokenizer": "ik_max_word"},
        "kb_query": {"type": "custom", "tokenizer": "ik_smart"}
      }
    }
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "knowledge_type": {"type": "keyword"},
      "project":        {"type": "keyword"},
      "equipment":      {"type": "keyword"},
      "error_codes":    {"type": "keyword"},

      "source_file":  {"type": "keyword", "index": false},
      "source_pages": {"type": "keyword", "index": false},
      "sections":     {"type": "object", "enabled": false},
      "summary":      {"type": "keyword", "index": false},

      "title": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart",
        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}
      },
      "body": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart"
      },

      "title_vec": {"type": "dense_vector", "dims": 1024, "index": true,
                    "similarity": "cosine", "index_options": {"type": "hnsw"}},
      "body_vec":  {"type": "dense_vector", "dims": 1024, "index": true,
                    "similarity": "cosine", "index_options": {"type": "hnsw"}},

      "created_at": {"type": "date"},
      "updated_at": {"type": "date"}
    }
  }
}
```

Field-by-field:

| Field | ES type | Key attributes | Role |
|---|---|---|---|
| `knowledge_type` | keyword | — | filter / exact match |
| `project` | keyword | — | filter; facet aggregation |
| `equipment` | keyword | — | filter; facet aggregation |
| `error_codes` | keyword | multi-valued | filter (`terms`, match-any); facet aggregation |
| `source_file` | keyword | `index: false` | stored, display-only |
| `source_pages` | keyword | `index: false` | stored, display-only |
| `summary` | keyword | `index: false` | ≤50-char digest, display-only |
| `sections` | object | `enabled: false` | raw section dict, returned verbatim, never indexed |
| `title` | text | `analyzer`/`search_analyzer`; sub-field `title.keyword` (`ignore_above:256`) | BM25 (with `title^N` boost) + vector; `.keyword` for exact sort |
| `body` | text | `analyzer`/`search_analyzer` | primary BM25 recall field |
| `title_vec` | dense_vector | `dims`, `index:true`, `cosine`, `hnsw` | title vector (not currently scored, reserved) |
| `body_vec` | dense_vector | `dims`, `index:true`, `cosine`, `hnsw` | target field for rescore & kNN |
| `created_at` / `updated_at` | date | — | audit timestamps |

### Analyzer selection {#analyzers}

- `_analyzer_settings()` emits the `settings.analysis` block (defining `kb_index` /
  `kb_query` custom analyzers) **only** when an IK tokenizer (`ik_max_word` /
  `ik_smart`) is requested.
- The mapping's `title`/`body` `analyzer` is set to the passed-in `index_analyzer` /
  `query_analyzer` names (default `ik_max_word` / `ik_smart`) — the IK plugin registers
  those analyzer names itself.
- **Fallback without the IK plugin**: set both `KB_ES__ANALYZER_INDEX` and
  `KB_ES__ANALYZER_QUERY` to `cjk` (ES built-in CJK bigram analysis); no custom settings
  block is emitted.
- `dims` comes from `KB_EMBEDDING__DIMS` (default 1024). **Both vector fields' dims must
  equal the embedding model's output dimension**, or writes fail with a dim mismatch.

### Index naming & aliases {#index-naming}

`src/kb/es/mappings.py`:

| Function | Returns | Example (prefix=`kb`) |
|---|---|---|
| `index_name(prefix, kt, version=1)` | `{prefix}_{kt}_v{version}` | `kb_alarm_v1` |
| `alias_name(prefix, kt)` | `{prefix}_{kt}` | `kb_alarm` |
| `all_alias_pattern(prefix)` | `{prefix}_*` | `kb_*` |

Physical indices are versioned (`kb_alarm_v1`); a stable alias (`kb_alarm`) points to
them. Reindex creates a new versioned index and **atomically swaps the alias**
(`create_one` / `reindex` in `src/kb/es/migrations.py`), enabling zero-downtime mapping
or embedding-model changes. At search time `_index_for()` uses the single alias when
`knowledge_type` is set, else a comma-joined list of all aliases for cross-type search.

---

## Auxiliary indices {#auxiliary-indices}

Two auxiliary indices are **never reseeded**, so they persist across restarts.

### `kb_import_files` — import tracker {#kb-import-files}

`src/kb/es/import_mappings.py`. De-duplicates by file content SHA-256 and stores the full
ES source payloads of committed documents so startup `restore_imports()` can re-index
them after reseed clears the main indices.

`settings`: `number_of_shards: 1`, `number_of_replicas: 0`; `mappings.dynamic: "strict"`.

| Field | Type | Notes |
|---|---|---|
| `file_hash` | keyword | file content SHA-256, also the document `_id` |
| `file_name` | keyword | original filename |
| `file_path` | keyword | on-disk path (for failed-file retry) |
| `file_size` | long | bytes |
| `file_type` | keyword | extension |
| `import_status` | keyword | `pending` / `committed` / `failed` |
| `committed_docs` | nested | sub-fields: `_index`(keyword), `_id`(keyword), `_source`(object, `enabled:false`) |
| `error_message` | text | `index: false` |
| `created_at` / `updated_at` | date | timestamps |

A `committed` row is **write-protected**: `record_pending` (an atomic upsert) and
`record_failed` both run a painless script guarded by `_PRESERVE_COMMITTED` that
no-ops on a committed row, so an abandoned or failed `force` re-import can't wipe the
`committed_docs` payload that `restore_imports()` replays after a reseed.

### `kb_search_feedback` — search feedback {#kb-search-feedback}

`src/kb/es/feedback_mappings.py`. Lightweight 👍/👎 signals, **purely observational** —
never fed back into search.

`settings`: `number_of_shards: 1`, `number_of_replicas: 0`; `mappings.dynamic: "strict"`.

| Field | Type |
|---|---|
| `doc_id` | keyword |
| `helpful` | boolean |
| `query_text` | keyword |
| `knowledge_type` / `project` / `equipment` / `search_status` | keyword |
| `request_id` | keyword |
| `created_at` | date |

---

## Search API models {#search-models}

`src/kb/models/search.py`.

### `SearchStatus` (enum) {#search-status}

| Value | Meaning |
|---|---|
| `strict_hit` | all filters + AND-keywords matched, within `strict_max_hits` |
| `too_many` | strict matched more than `strict_max_hits`; ask the user to narrow |
| `loose_hit` | fell back to OR-keywords; must carry a "for reference only" banner |
| `vector_only` | only vector similarity matched (low confidence) |
| `no_hit` | nothing matched |

`SearchMode = Literal["auto", "strict", "loose", "vector_only"]`.

### `SearchRequest` {#search-request}

| Field | Type | Default | Constraint |
|---|---|---|---|
| `knowledge_type` | `KnowledgeType \| None` | `None` | None → search all indices |
| `project` | `str \| None` | `None` | `max_length=200` |
| `equipment` | `str \| None` | `None` | `max_length=200` |
| `error_codes` | `list[str]` | `[]` | list `max_length=64`, each item `max_length=64` |
| `keywords` | `list[str]` | `[]` | list `max_length=64`, each item `max_length=200` |
| `query_text` | `str \| None` | `None` | `max_length=4000`; raw text needed for vector recall/rescore |
| `mode` | `SearchMode` | `"auto"` | see above |
| `size` | `int` | `10` | `1 ≤ size ≤ 50` |
| `from_` | `int` | `0` | `≥ 0` |

Model-level validation: `from_ + size ≤ 10000` (`_MAX_RESULT_WINDOW`, mirrors ES
`index.max_result_window`); exceeding it returns 400 at the model rather than an opaque
ES error.

### `DocHit` {#doc-hit}

| Field | Type | Notes |
|---|---|---|
| `id` | str | ES `_id` |
| `score` | float | final score |
| `knowledge_type` | `KnowledgeType` | |
| `project` / `equipment` | str | |
| `error_codes` | `list[str]` | |
| `title` | str | |
| `source_file` | `str \| None` | |
| `source_pages` | `list[str]` | |
| `summary` | `str \| None` | ≤50 chars |
| `sections` | `dict[str, str]` | original sections, verbatim, never AI-rewritten |

### `SearchResponse` {#search-response}

| Field | Type | Notes |
|---|---|---|
| `status` | `SearchStatus` | see status contract |
| `total` | int | total hits |
| `hits` | `list[DocHit]` | empty on `too_many`/`no_hit` |
| `effective_params` | `EffectiveParams` | normalized params actually applied (echoed for upstream display) |
| `facets` | `dict[str, dict[str, int]]` | only on `too_many`: project/equipment/error_codes bucket counts |
| `facets_truncated` | `dict[str, int]` | per-facet count outside the top buckets (ES `sum_other_doc_count`); non-zero means the bucket list is not exhaustive |
| `banner` | `str \| None` | must be rendered verbatim on `loose_hit`/`vector_only`/`no_hit` |

`EffectiveParams` fields: `knowledge_type`, `project`, `equipment`, `error_codes`,
`keywords`.

Fixed banner strings (`src/kb/services/search.py`):

| Case | Text |
|---|---|
| `loose_hit` | 没有完全匹配的知识，以下为相关参考，仅供参考。 |
| `vector_only` | 没有关键词匹配的知识，以下基于语义相似度的相关参考，仅供参考。 |
| `no_hit` | 没有找到匹配的知识。请补充关键词或调整筛选条件后重试。 |

---

## Import pipeline models {#ingest-models}

`src/kb/models/ingest.py`.

### Status enums

- `ImportStatus`: `pending` / `extracting` / `ready_for_review` / `committed` / `failed`
- `FileStatus`: `processing` / `skipped_duplicate` / `unsupported` / `failed` / `done`

### `StagedDocument` (under review)

An editable document during preview, converted back to a `KnowledgeDoc` and validated on
commit. Key fields: `index`, `knowledge_type`, `project`, `equipment`, `title`,
`error_codes`, the per-type content fields (`content`/`resolution`,
`procedure`/`prerequisites`, `body_text`), `notes`, `source_file`, `source_pages`,
`raw_text_excerpt`, `confidence` (0–1), `warnings`, `accepted` (default True).

### Other models

- `SkippedChunk`: `source_file`, `page_range`, `reason` (`non_content`/`no_entries`/`parse_failed`), `hint`
- `FileInfo`: per-file status + segmentation progress (`chunks_total`/`chunks_done` — `chunks_done` counts **completed** chunks/`skipped_chunks`) plus an optional `duplicate_info` set only when `status == skipped_duplicate`
- `DuplicateInfo` / `DuplicateDocSummary`: what the KB already holds for a re-uploaded file — `imported_at`, `original_file_name`, total `doc_count`, and a ≤50-entry `documents` preview (`knowledge_type`, `title`, `error_codes`). Built from the existing tracker record, no extra ES read
- `ImportSession`: the whole session (`session_id`, `status`, `files`, `documents`, the `*_hint`s, `created_at`)
- Request/response: `UploadResponse`, `ScanRequest`, `SessionResponse`, `SessionListItem`,
  `DocumentUpdate`, `AcceptReject`, `AcceptAllRequest`, `RetryRequest`, `CommitResponse`,
  `RecommitTrackingResponse` (field details in the [API Reference](../api-reference.md)).

---

## Taxonomy model {#taxonomy-model}

`Taxonomy` in `src/kb/models/taxonomy.py`:

| Field | Type | Constraint |
|---|---|---|
| `version` | str | opaque string, not interpreted by the engine; bump it manually on change |
| `knowledge_types` | `list[KnowledgeType]` | |
| `projects` | `list[str]` | `min_length=1`; non-blank, no surrounding whitespace, unique |
| `equipment` | `list[str]` | `min_length=1`; non-blank, no surrounding whitespace, unique |

Helpers: `has_project(p)`, `has_equipment(e)`. The `_no_blanks` validator rejects blank
entries, entries with surrounding whitespace, and duplicates.

At startup `_sync_taxonomy_from_es()` aggregates the project/equipment values actually
present in ES, appends any missing values back into `config/taxonomy.yaml`, and rewrites
`version` to `auto-<timestamp>` — so that file must be writable at runtime.

---

## CSV column → document field mapping {#csv-mapping}

The startup seeder (`src/kb/services/csv_loader.py`) reads documents from three CSVs in
`config/`. Column names are Chinese; encoding is `utf-8-sig`.

### `机台报警_header.csv` → `AlarmDoc`

`项目,机台,代码,英文标题,中文标题,内容,解除流程,注意事项,ppt文件,ppt页面`

| CSV column | Field | Handling |
|---|---|---|
| 项目 | `project` | strip |
| 机台 | `equipment` | strip |
| 代码 | `error_codes` | split on `[\s,，;&、]+` into multiple codes |
| 中文标题 + 英文标题 | `title` | `中文（英文）`, truncated to 200 |
| 内容 | `content` | `—` if blank |
| 解除流程 | `resolution` | `—` if blank |
| 注意事项 | `notes` | |
| ppt文件 | `source_file` | |
| ppt页面 | `source_pages` | split on `,` |

`summary` = first non-empty line of `content` (truncated to 50).

### `机台setup_header.csv` → `SetupDoc`

`项目,设备,工站/部件/站位,规格/要求,调试步骤,调试工具,注意事项,ppt文件,PPT页面`

| CSV column | Field | Handling |
|---|---|---|
| 项目 | `project` | |
| 设备 | `equipment` | |
| 工站/部件/站位 | `title` | `{设备} · {station} 调试` (or `{设备} 调试` if no station) |
| 规格/要求 + 调试工具 | `prerequisites` | one line each: `规格/要求：…` / `调试工具：…` |
| 调试步骤 | `procedure` | `—` if blank |
| 注意事项 | `notes` | |
| ppt文件 | `source_file` | |
| PPT页面 | `source_pages` | |

### `设备经验_header.csv` → `ExperienceDoc`

`项目,机台,问题,失败描述,失败分析,根因,纠正步骤,PPT文件,PPT页面`

| CSV column | Field | Handling |
|---|---|---|
| 项目 | `project` | |
| 机台 | `equipment` | |
| 问题 | `title` | truncated to 200 |
| 失败描述 | `body_text` | opening paragraph |
| 失败分析 | `body_text` | appended `【失败分析】…` |
| 根因 | `body_text` | appended `【根因】…` |
| 纠正步骤 | `procedure` | |
| PPT文件 | `source_file` | |
| PPT页面 | `source_pages` | |

A missing CSV logs one warning and is skipped; a row that fails Pydantic validation is
skipped (row only).

---

## Knowledge-type spec schema {#type-spec}

`config/knowledge_types/<type>.yaml` is the **single source of truth** between the LLM
segmentation prompt and the `Doc` model (loaded by `src/kb/services/spec.py`, cached
process-wide via `lru_cache`). All three files must exist or loading raises.

Top-level keys:

| Key | Type | Notes |
|---|---|---|
| `type` | str | a `KnowledgeType` value |
| `display_name` | str | type name shown in the prompt |
| `summary_zh` / `summary_en` | str | one-liner for the router prompt (legacy `summary` accepted) |
| `csv_source` | str | corresponding CSV path (docs/example only) |
| `fields` | list | see below |
| `boundary_hints` | list[str] | entry-boundary cues |
| `confidence_guide` | str | confidence scoring guidance |
| `skip_if` | list[str] | when to return an empty array `[]` |
| `example_input` / `example_output` | str / list[dict] | worked sample fed to the LLM |

Each `fields[]` entry (`FieldSpec`): `name`, `desc`, `label_zh`, `csv_column`,
`required` (bool). A `required: true` field enters segmentation's "required-field check"
— an extracted entry whose required field is empty (`""`/`—`/`-`/`n/a` …) or whose
confidence is < 0.3 is dropped.

`render_segmentation_prompt(spec)` and `render_router_prompt(specs)` render the spec into
LLM system prompts (see [Import Pipeline](../architecture/import-pipeline.md) and
[AI Chat Search](../architecture/ai-chat.md)).
