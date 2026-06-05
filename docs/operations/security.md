# Security & Data Handling {#security}

This service is designed for a **trusted internal network**. It ships with no
authentication and assumes the documents it serves are non-sensitive manufacturing
knowledge. This page documents the boundaries it *does* enforce, the ones it *doesn't*,
and how to harden a real deployment.

---

## Threat model in one line {#model}

The app trusts its callers but not its **inputs**: uploaded files and scan paths are
treated as hostile (bounded, sanitized), while *who* is calling is assumed to be already
authorized by an upstream gateway.

---

## What the app enforces {#enforced}

### Server-side scan boundary {#scan-boundary}

`POST /api/v1/ingest/scan` resolves `folder_path` **under `ingest.scan_root`** (default
`data`). A path that escapes that root (`../`, absolute paths) is rejected with **400** —
a caller cannot use the scan endpoint to read arbitrary host files.

### Upload path sanitization

Uploaded filenames are sanitized before they touch the filesystem: traversal sequences are
stripped and the destination is forced to stay inside `ingest.upload_dir`
(`data/uploads`). This is covered by `tests/unit/test_import_security.py`
(`test_safe_upload_path_strips_traversal`, `…keeps_plain_name_inside_dir`).

### Resource bounds (DoS guards)

Pathological files can't exhaust memory during extraction — each bound rejects with a
clear error instead of risking an OOM that takes the process down:

| Bound | Setting | Default |
|---|---|---|
| Per-file size | `ingest.max_file_size_mb` | 50 MB |
| Allowed types | `ingest.allowed_extensions` | pdf, xlsx, xls, csv, pptx, docx |
| PDF pages | `ingest.pdf_max_pages` | 2000 |
| XLSX cells | `ingest.xlsx_max_cells` | 2 000 000 |

Request payloads are bounded too: search keywords/error-codes are length- and
count-capped (`src/kb/models/search.py`), and chat is capped at 200 messages × 20 000
chars each (`src/kb/api/chat.py`) so a caller can't drive unbounded memory or LLM token
cost. Import sessions are TTL-evicted (`session_ttl_minutes` / `session_hard_ttl_minutes`)
so abandoned previews can't pin memory.

### Taxonomy as an allow-list

`project`/`equipment` are validated against `config/taxonomy.yaml` at index time, and
LLM-extracted values not in the taxonomy are dropped rather than used as filters. This
keeps both stored data and queries within a known vocabulary.

### Zero-fabrication as a safety property

Search results are verbatim documents or nothing — the LLM never injects generated text
into results. This is an architectural guarantee (see
[Architecture → constraints](../architecture/overview.md)), and it means the system cannot
"make up" a procedure or a parameter value: a wrong answer can only ever be a wrong
*retrieval*, which is auditable via the stored `request_id`.

---

## What the app does NOT provide {#not-provided}

| Gap | Implication | Mitigation |
|---|---|---|
| **Authentication / authorization** | Any caller who can reach the port can search, ingest, and delete | Deploy behind an API gateway / reverse proxy that authenticates and authorizes; never expose `:8000` publicly |
| **Multi-tenancy / per-user scoping** | All documents are visible to all callers | Segregate at the network layer, or run separate instances per tenant |
| **Encryption at rest** | ES data on the `es-data` volume is unencrypted by default | Use encrypted storage / an ES security tier |
| **Admin-endpoint gating** | `/api/v1/admin/*` and `DELETE` are unauthenticated like everything else | Restrict these paths at the gateway |
| **Rate limiting** | No built-in throttle | Enforce at the gateway/proxy |

---

## Secrets {#secrets}

The only secrets are the upstream API keys and ES credentials:

- `KB_LLM__API_KEY`, `KB_EMBEDDING__API_KEY`, `KB_ES__PASSWORD`.
- Supply them via `.env` (git-ignored) or your orchestrator's secret store — **never** bake
  them into the image or commit them. `.env.example` is the committed template.
- Missing keys degrade features rather than crash (no LLM → 503 on AI endpoints; no
  embedding key → BM25-only), so it's safe to run without them in a locked-down tier.

---

## Transport & ES auth {#transport}

For ES over an untrusted network:

```bash
KB_ES__URL=https://es.internal:9200
KB_ES__VERIFY_CERTS=true
KB_ES__SSL_FINGERPRINT=<sha256-from-es-startup>   # for a self-signed node
KB_ES__USERNAME=kb_app
KB_ES__PASSWORD=<from-secret-store>
```

Use a least-privilege ES user scoped to the `kb_*` indices. See
[Deployment → Hardening](deployment.md#hardening) for the full production checklist.

---

## Auditing {#audit}

Every request carries a `request_id` propagated into logs and onto search-feedback records,
so a result (including a 👎) can be traced back to the exact query, extracted params, and
upstream calls that produced it — see [Observability → Logging](../observability.md#logging).
There is no built-in access log of *who* read *which* document (no identity layer); capture
that at the gateway if you need it.
