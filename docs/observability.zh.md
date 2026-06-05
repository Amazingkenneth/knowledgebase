# 可观测性

应用内置结构化日志、Prometheus 指标，以及一个观察用的搜索反馈信号。在[设置](configuration.md#observability)的 `observability:` 段配置。

---

## 日志

`src/kb/observability/logging_config.py` 配置根日志器。

- `observability.json_logs = true` → 每行一个 JSON 对象（机器可读）。
- `observability.json_logs = false`（默认）→ 仍携带请求 id 的人类可读行。
- `observability.log_level` 设置级别（默认 `INFO`）。

每个请求都被打上 `request_id`（见 `observability/middleware.py`），传播到日志并存入反馈记录，因此一次 👎 可追溯到产生它的那次具体搜索。

---

## 指标

当 `observability.metrics_enabled = true`（默认）时，Prometheus 指标在以下位置暴露：

```
GET /metrics
```

应用通过 `src/kb/observability/metrics.py` 中的辅助函数（`measure_upstream(...)`、`record_upstream_error(...)`）记录其依赖项——Elasticsearch、embedding 服务、LLM——的上游调用延迟与错误计数。一次静默的 embedding 服务故障正是这样显现的：搜索照常用 BM25 成功，但 `embedding` 上游错误计数器持续攀升。

将 Prometheus 抓取任务指向 `/metrics`，并对上升的上游错误计数器或延迟告警。

---

## 搜索反馈 {#search-feedback}

针对单条搜索结果的轻量 👍/👎 信号，记录在 `kb_search_feedback` 索引（`src/kb/api/feedback.py`）。它**仅观察用**——绝不改变搜索结果，因此零编造契约依然成立。

### 记录一次信号

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

记录是尽力而为的：存储故障返回 **503** 而非打断用户流程。记录还会存入请求的 `request_id` 与 UTC 时间戳。

### 读取聚合

```
GET /api/v1/admin/search-feedback?limit=20
```

返回 `FeedbackSummary`：

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

用有用率与表现最差的查询，判断是否需要调 `title_boost`、`vector_weight` 或 `rrf_window`——见[检索与排序](architecture/search-ranking.md#the-ranking-formula)。
