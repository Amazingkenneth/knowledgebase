# 安全与数据处理 {#security}

本服务面向**可信内网**设计。它默认不带认证，并假定其服务的文档是非敏感的制造知识。本页说明它*确实*
强制的边界、*不*强制的边界，以及如何为真实部署加固。

---

## 一句话威胁模型 {#model}

应用信任其调用方，但不信任其**输入**：上传文件与扫描路径被当作敌意输入（受限、净化），而*谁*在调用
则假定已由上游网关授权。

---

## 应用强制的内容 {#enforced}

### 服务端扫描边界 {#scan-boundary}

`POST /api/v1/ingest/scan` 在 **`ingest.scan_root`（默认 `data`）之下**解析 `folder_path`。逃逸该
根目录的路径（`../`、绝对路径）以 **400** 拒绝 —— 调用方无法借扫描接口读取任意宿主文件。

### 上传路径净化

上传文件名在落盘前被净化：穿越序列被剥除，目标被强制限制在 `ingest.upload_dir`（`data/uploads`）
之内。此项由 `tests/unit/test_import_security.py` 覆盖（`test_safe_upload_path_strips_traversal`、
`…keeps_plain_name_inside_dir`）。

### 资源边界（DoS 护栏）

病态文件无法在抽取期耗尽内存 —— 每个边界以清晰错误拒绝，而非冒着拖垮进程的 OOM 风险：

| 边界 | 设置 | 默认 |
|---|---|---|
| 单文件大小 | `ingest.max_file_size_mb` | 50 MB |
| 允许类型 | `ingest.allowed_extensions` | pdf、xlsx、xls、csv、pptx、docx |
| PDF 页数 | `ingest.pdf_max_pages` | 2000 |
| XLSX 单元格 | `ingest.xlsx_max_cells` | 2 000 000 |

请求载荷也受限：搜索关键词/报警码受长度与数量上限约束（`src/kb/models/search.py`），对话上限为
200 条消息 × 每条 20 000 字符（`src/kb/api/chat.py`），使调用方无法驱动无界内存或 LLM token 成本。
导入会话按 TTL 回收（`session_ttl_minutes` / `session_hard_ttl_minutes`），使被遗弃的预览无法占用内存。

### taxonomy 作为白名单

`project`/`equipment` 在索引时对照 `config/taxonomy.yaml` 校验，LLM 抽取出的非 taxonomy 值会被丢弃
而非用作过滤。这使存储数据与查询都落在已知词汇内。

### 零编造作为安全属性

搜索结果要么是逐字文档、要么什么都没有 —— LLM 绝不向结果注入生成文本。这是架构层面的保证（见
[架构 → 约束](../architecture/overview.zh.md)），意味着系统无法"编造"流程或参数值：错误的答案至多只能
是错误的*检索*，而这可通过存储的 `request_id` 审计。

---

## 应用**不**提供的内容 {#not-provided}

| 缺口 | 影响 | 缓解 |
|---|---|---|
| **认证 / 授权** | 任何能触达端口的调用方都能搜索、导入、删除 | 部署在执行认证授权的 API 网关/反向代理之后；切勿公开暴露 `:8000` |
| **多租户 / 按用户隔离** | 所有文档对所有调用方可见 | 在网络层隔离，或为每租户运行独立实例 |
| **静态加密** | `es-data` 卷上的 ES 数据默认不加密 | 使用加密存储 / ES 安全分层 |
| **管理接口门禁** | `/api/v1/admin/*` 与 `DELETE` 同样无鉴权 | 在网关限制这些路径 |
| **限流** | 无内置节流 | 在网关/代理层执行 |

---

## 密钥 {#secrets}

唯一的密钥是上游 API 密钥与 ES 凭据：

- `KB_LLM__API_KEY`、`KB_EMBEDDING__API_KEY`、`KB_ES__PASSWORD`。
- 经 `.env`（git 忽略）或编排器密钥库提供 —— **切勿**烤进镜像或提交。`.env.example` 是已提交的模板。
- 缺失密钥会降级功能而非崩溃（无 LLM → AI 接口 503；无嵌入密钥 → 仅 BM25），因此在锁定分层中不带
  密钥运行是安全的。

---

## 传输与 ES 鉴权 {#transport}

对于经不可信网络访问的 ES：

```bash
KB_ES__URL=https://es.internal:9200
KB_ES__VERIFY_CERTS=true
KB_ES__SSL_FINGERPRINT=<es-启动输出的-sha256>   # 自签节点
KB_ES__USERNAME=kb_app
KB_ES__PASSWORD=<来自密钥库>
```

使用范围限定在 `kb_*` 索引的最小权限 ES 用户。完整生产清单见
[部署 → 加固](deployment.zh.md#hardening)。

---

## 审计 {#audit}

每个请求都带一个 `request_id`，贯穿日志并记录到搜索反馈记录中，因此一个结果（含一次 👎）能被追溯到
产生它的确切查询、抽取参数与上游调用 —— 见[可观测性 → 日志](../observability.zh.md#logging)。系统不内置
*谁*读了*哪份*文档的访问日志（无身份层）；若有需要，请在网关层捕获。
