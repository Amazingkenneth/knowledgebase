# Observability

The app ships with structured logging, Prometheus metrics, an observational
search-feedback signal, and liveness/readiness probes. Everything here is configured under
`observability:` in [settings](configuration.md#observability) and implemented in
`src/kb/observability/`.

---

## Logging {#logging}

`src/kb/observability/logging_config.py` configures the root logger. Two toggles:

| Setting | Default | Effect |
|---|---|---|
| `observability.json_logs` | `false` | `true` → one JSON object per line (machine-readable) |
| `observability.log_level` | `INFO` | Standard Python level name |

Every request is tagged with a `request_id` (a UUID set by
`observability/middleware.py`), propagated into every log line emitted while handling that
request and stored on feedback records — so a 👎 can be traced back to the exact search
that produced it.

**Human-readable line** (`json_logs = false`):

```
2026-06-05 09:14:22 INFO [kb.chat] req=3f9c1a2e search status=strict_hit total=2
```

**JSON line** (`json_logs = true`) — point your log shipper at stdout and parse one object
per line:

```json
{"ts":"2026-06-05T09:14:22.481Z","level":"INFO","logger":"kb.chat","request_id":"3f9c1a2e","message":"search status=strict_hit total=2"}
```

!!! tip "Tracing a bad result"
    A `kb_search_feedback` record stores the same `request_id`. Grep your logs for that id
    to replay exactly which params were extracted, which ES query ran, and which upstream
    calls (LLM/embedding) happened on that turn.

---

## Metrics {#metrics}

When `observability.metrics_enabled = true` (default), Prometheus metrics are exposed in
text format at:

```
GET /metrics
```

All collectors are module-level singletons in `src/kb/observability/metrics.py`. HTTP
metrics are recorded by `MetricsMiddleware`; upstream metrics by the
`measure_upstream(...)` / `record_upstream_error(...)` helpers that wrap calls to ES, the
embedding service, and the LLM; import metrics by the pipeline.

### Exposed instruments {#instruments}

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `kb_http_requests_total` | Counter | `method`, `path`, `status` | Total HTTP requests handled |
| `kb_http_request_duration_seconds` | Histogram | `method`, `path` | Request latency |
| `kb_http_requests_in_progress` | Gauge | — | Requests currently being served |
| `kb_upstream_errors_total` | Counter | `service` = `llm`\|`embedding`\|`es` | Errors talking to a dependency |
| `kb_upstream_latency_seconds` | Histogram | `service` = `llm`\|`embedding`\|`es` | Latency of dependency calls |
| `kb_import_files_total` | Counter | `status` = `done`\|`failed`\|`skipped_duplicate`\|`unsupported` | Files reaching a terminal import status |
| `kb_import_docs_total` | Counter | `outcome` = `extracted`\|`committed`\|`rejected` | Staged documents by outcome |
| `kb_import_commit_duration_seconds` | Histogram | — | Wall-clock time to commit an import session |

Histograms also expose the usual `_bucket`, `_sum`, and `_count` series, so you get rate,
average, and quantiles for free.

### How a silent degradation surfaces {#degradation-signal}

The embedding service is optional: when it's unreachable, searches keep succeeding on
BM25 (no error to the user). The signal is the metric — `kb_upstream_errors_total{service="embedding"}`
climbs while `kb_http_requests_total{path="/api/v1/search",status="200"}` keeps rising.
Alert on the **upstream** counter, not on HTTP 5xx.

### Scrape config {#scrape}

```yaml
# prometheus.yml
scrape_configs:
  - job_name: knowledge-base
    metrics_path: /metrics
    static_configs:
      - targets: ["kb-app:8000"]
```

### Example alert rules {#alerts}

```yaml
groups:
  - name: knowledge-base
    rules:
      # Embedding outage: vector search silently degraded to BM25.
      - alert: KBEmbeddingDegraded
        expr: rate(kb_upstream_errors_total{service="embedding"}[5m]) > 0
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "Embedding upstream erroring — search is BM25-only"

      # p99 request latency over 2s for 10 minutes.
      - alert: KBHighLatency
        expr: |
          histogram_quantile(0.99,
            sum(rate(kb_http_request_duration_seconds_bucket[5m])) by (le)) > 2
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "p99 API latency above 2s"

      # Import failures.
      - alert: KBImportFailures
        expr: increase(kb_import_files_total{status="failed"}[15m]) > 0
        labels: { severity: info }
        annotations:
          summary: "Files failing the import pipeline"
```

Use these alongside the [search-feedback](#search-feedback) signal: metrics tell you the
*system* is healthy, feedback tells you the *results* are good.

---

## Health endpoints {#health}

Two probes (defined in `src/kb/main.py`), meant for orchestrators and the Docker
`HEALTHCHECK`:

| Endpoint | Probes | Returns |
|---|---|---|
| `GET /healthz` | Nothing — liveness only | Always `200 {"status":"ok"}` |
| `GET /readyz` | Pings Elasticsearch | `200` when ES reachable, else `503` (degraded) |
| `GET /readyz?deep=true` | Also round-trips the embedding service | adds `embedding: ok\|down` |

`/readyz` body:

```json
{ "status": "ok", "es": "ok", "embedding": "configured", "llm": "configured" }
```

- `embedding`: `disabled` (no key) · `configured` (key set, not probed) · `ok`/`down`
  (only with `?deep=true`).
- `llm`: `configured` or `disabled`.

The default `/readyz` stays cheap (ES ping only) so it's safe on every healthcheck tick;
reserve `?deep=true` for occasional checks since it costs an upstream embedding call. The
compose/Docker `HEALTHCHECK` targets plain `/readyz`. On startup, if ES is unreachable the
service comes up **DEGRADED** (search/index fail until ES recovers) rather than refusing to
boot — see [Build from Scratch → Startup](reference/build-from-scratch.md#startup).

---

## Search feedback {#search-feedback}

A lightweight 👍/👎 signal on individual search results, captured in the
`kb_search_feedback` index (`src/kb/api/feedback.py`). It is **observational only** — it
never alters search results, so the zero-fabrication contract stands.

### Recording a signal

```
POST /api/v1/search/feedback   →  202 Accepted
```

```json
{
  "doc_id": "kb_alarm_v1:abc123",
  "helpful": false,
  "query_text": "E-1234 aligner fault",
  "knowledge_type": "alarm",
  "project": "PDX",
  "equipment": "Aligner",
  "search_status": "loose_hit"
}
```

Recording is best-effort: a storage hiccup returns **503** rather than breaking the user's
flow. The stored document adds the request's `request_id` and a UTC `created_at` timestamp
to the fields above (full mapping in
[Data Model → Feedback index](reference/data-model.md)).

### Reading the aggregate

```
GET /api/v1/admin/search-feedback?limit=20
```

Returns a `FeedbackSummary` (the ES aggregation counts the boolean `helpful` field and the
top unhelpful `query_text` values):

```json
{
  "total": 142,
  "helpful": 118,
  "unhelpful": 24,
  "helpful_ratio": 0.83,
  "top_unhelpful_queries": [
    { "query": "heater overtemp", "unhelpful": 5 }
  ]
}
```

Use the helpful ratio and the worst-performing queries to decide whether to tune
`title_boost`, `vector_weight`, or `rrf_window` — see
[Search & Ranking](architecture/search-ranking.md#the-ranking-formula).
