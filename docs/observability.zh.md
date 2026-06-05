# 可观测性

应用内置结构化日志、Prometheus 指标、观测型搜索反馈信号，以及存活/就绪探针。相关配置位于
[settings](configuration.zh.md#observability) 的 `observability:` 块，实现位于
`src/kb/observability/`。

---

## 日志 {#logging}

`src/kb/observability/logging_config.py` 配置根 logger。两个开关：

| 配置 | 默认 | 作用 |
|---|---|---|
| `observability.json_logs` | `false` | `true` → 每行一个 JSON 对象（机器可读） |
| `observability.log_level` | `INFO` | 标准 Python 级别名 |

每个请求都带一个 `request_id`（由 `observability/middleware.py` 生成的 UUID），贯穿该请求处理
期间的每条日志，并记录到反馈记录中 —— 这样一次 👎 就能追溯到产生它的那次具体搜索。

**人类可读行**（`json_logs = false`）：

```
2026-06-05 09:14:22 INFO [kb.chat] req=3f9c1a2e search status=strict_hit total=2
```

**JSON 行**（`json_logs = true`）—— 将日志采集器对准 stdout，按行解析每个对象：

```json
{"ts":"2026-06-05T09:14:22.481Z","level":"INFO","logger":"kb.chat","request_id":"3f9c1a2e","message":"search status=strict_hit total=2"}
```

!!! tip "追溯一个糟糕的结果"
    `kb_search_feedback` 记录里存有同一个 `request_id`。用该 id 在日志中检索，即可重放那一轮
    抽取了哪些参数、执行了哪条 ES 查询、发生了哪些上游调用（LLM/嵌入）。

---

## 指标 {#metrics}

当 `observability.metrics_enabled = true`（默认）时，Prometheus 指标以文本格式暴露在：

```
GET /metrics
```

所有 collector 均为 `src/kb/observability/metrics.py` 中的模块级单例。HTTP 指标由
`MetricsMiddleware` 记录；上游指标由包裹 ES、嵌入服务和 LLM 调用的
`measure_upstream(...)` / `record_upstream_error(...)` 辅助函数记录；导入指标由管道记录。

### 暴露的指标 {#instruments}

| 指标 | 类型 | 标签 | 含义 |
|---|---|---|---|
| `kb_http_requests_total` | Counter | `method`、`path`、`status` | 处理的 HTTP 请求总数 |
| `kb_http_request_duration_seconds` | Histogram | `method`、`path` | 请求时延 |
| `kb_http_requests_in_progress` | Gauge | — | 当前正在处理的请求数 |
| `kb_upstream_errors_total` | Counter | `service` = `llm`\|`embedding`\|`es` | 与依赖通信的错误 |
| `kb_upstream_latency_seconds` | Histogram | `service` = `llm`\|`embedding`\|`es` | 依赖调用时延 |
| `kb_import_files_total` | Counter | `status` = `done`\|`failed`\|`skipped_duplicate`\|`unsupported` | 到达终态的导入文件 |
| `kb_import_docs_total` | Counter | `outcome` = `extracted`\|`committed`\|`rejected` | 按结果分类的暂存文档 |
| `kb_import_commit_duration_seconds` | Histogram | — | 提交一次导入会话的耗时 |

Histogram 还会暴露常规的 `_bucket`、`_sum`、`_count` 序列，因此速率、均值、分位数都开箱即用。

### 静默降级如何显现 {#degradation-signal}

嵌入服务是可选的：不可达时，搜索仍以 BM25 成功（对用户无报错）。信号在指标里 ——
`kb_upstream_errors_total{service="embedding"}` 上升，而
`kb_http_requests_total{path="/api/v1/search",status="200"}` 仍在增长。应对**上游**计数器告警，
而非对 HTTP 5xx 告警。

### 抓取配置 {#scrape}

```yaml
# prometheus.yml
scrape_configs:
  - job_name: knowledge-base
    metrics_path: /metrics
    static_configs:
      - targets: ["kb-app:8000"]
```

### 告警规则示例 {#alerts}

```yaml
groups:
  - name: knowledge-base
    rules:
      # 嵌入服务故障：向量检索已静默降级为 BM25。
      - alert: KBEmbeddingDegraded
        expr: rate(kb_upstream_errors_total{service="embedding"}[5m]) > 0
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "嵌入上游报错 —— 搜索仅剩 BM25"

      # p99 请求时延超过 2s 持续 10 分钟。
      - alert: KBHighLatency
        expr: |
          histogram_quantile(0.99,
            sum(rate(kb_http_request_duration_seconds_bucket[5m])) by (le)) > 2
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "p99 API 时延超过 2s"

      # 导入失败。
      - alert: KBImportFailures
        expr: increase(kb_import_files_total{status="failed"}[15m]) > 0
        labels: { severity: info }
        annotations:
          summary: "有文件在导入管道中失败"
```

请与[搜索反馈](#search-feedback)信号配合使用：指标告诉你*系统*是否健康，反馈告诉你*结果*是否优质。

---

## 健康检查接口 {#health}

两个探针（定义于 `src/kb/main.py`），供编排器与 Docker `HEALTHCHECK` 使用：

| 接口 | 探测内容 | 返回 |
|---|---|---|
| `GET /healthz` | 无 —— 仅存活性 | 恒返回 `200 {"status":"ok"}` |
| `GET /readyz` | ping Elasticsearch | ES 可达返回 `200`，否则 `503`（降级） |
| `GET /readyz?deep=true` | 额外往返嵌入服务 | 增加 `embedding: ok\|down` |

`/readyz` 响应体：

```json
{ "status": "ok", "es": "ok", "embedding": "configured", "llm": "configured" }
```

- `embedding`：`disabled`（无密钥）· `configured`（已配置密钥但未探测）· `ok`/`down`
  （仅在 `?deep=true` 时）。
- `llm`：`configured` 或 `disabled`。

默认的 `/readyz` 保持低开销（仅 ES ping），适合每次健康检查节拍调用；`?deep=true` 因会产生一次
上游嵌入调用，仅供偶尔检查。compose/Docker 的 `HEALTHCHECK` 指向纯 `/readyz`。启动时若 ES 不可
达，服务会以 **DEGRADED**（降级）状态启动（搜索/索引在 ES 恢复前失败）而非拒绝启动 —— 见
[从零搭建 → 启动](reference/build-from-scratch.zh.md#startup)。

---

## 搜索反馈 {#search-feedback}

对单条搜索结果的轻量 👍/👎 信号，记录到 `kb_search_feedback` 索引（`src/kb/api/feedback.py`）。
它**仅作观测** —— 绝不改变搜索结果，因此零编造契约依然成立。

### 记录信号

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

记录尽力而为：存储故障返回 **503** 而非中断用户流程。存储的文档会在上述字段基础上追加请求的
`request_id` 和 UTC `created_at` 时间戳（完整映射见
[数据模型 → 反馈索引](reference/data-model.zh.md)）。

### 读取聚合

```
GET /api/v1/admin/search-feedback?limit=20
```

返回 `FeedbackSummary`（ES 聚合统计布尔字段 `helpful` 及最常见的不满意 `query_text`）：

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

依据满意率和表现最差的查询，判断是否需要调优 `title_boost`、`vector_weight` 或 `rrf_window`
—— 见[检索与排序](architecture/search-ranking.zh.md#the-ranking-formula)。
