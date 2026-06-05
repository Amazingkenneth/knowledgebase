# Observability

The app ships with structured logging, Prometheus metrics, and an observational
search-feedback signal. Configure under `observability:` in
[settings](configuration.md#observability).

---

## Logging

`src/kb/observability/logging_config.py` configures the root logger.

- `observability.json_logs = true` → one JSON object per line (machine-readable).
- `observability.json_logs = false` (default) → a human-readable line that still
  carries the request id.
- `observability.log_level` sets the level (default `INFO`).

Every request is tagged with a `request_id` (see `observability/middleware.py`),
propagated into logs and stored on feedback records so a 👎 can be traced back to the
exact search that produced it.

---

## Metrics

When `observability.metrics_enabled = true` (default), Prometheus metrics are
exposed at:

```
GET /metrics
```

The app records upstream call latency and error counts for its dependencies —
Elasticsearch, the embedding service, and the LLM — via the helpers in
`src/kb/observability/metrics.py` (`measure_upstream(...)`,
`record_upstream_error(...)`). This is how a silent embedding-service outage shows
up: searches keep succeeding on BM25, but the `embedding` upstream-error counter
climbs.

Point a Prometheus scrape job at `/metrics` and alert on rising upstream-error
counters or latency.

---

## Search feedback {#search-feedback}

A lightweight 👍/👎 signal on individual search results, captured in the
`kb_search_feedback` index (`src/kb/api/feedback.py`). It is **observational only** —
it never alters search results, so the zero-fabrication contract stands.

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

Recording is best-effort: a storage hiccup returns **503** rather than breaking the
user's flow. The record also stores the request's `request_id` and a UTC timestamp.

### Reading the aggregate

```
GET /api/v1/admin/search-feedback?limit=20
```

Returns a `FeedbackSummary`:

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
