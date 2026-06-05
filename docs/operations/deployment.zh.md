# 部署 {#deployment}

受支持的部署方式是 **Docker Compose**：一个自定义的 Elasticsearch 镜像（含 IK 分词器）加上
FastAPI 应用镜像。本页介绍 compose 栈、应用镜像、生产加固、数据持久化/备份，以及文档自动发布钩子。

---

## 快速开始 {#quickstart}

```bash
cp .env.example .env          # 填入 KB_LLM__API_KEY / KB_EMBEDDING__API_KEY（可选）
docker compose up -d --build  # 构建 ES+IK 与应用；应用监听 :8000
curl localhost:8000/readyz    # ES 可达后返回 200
```

`docker compose up -d --build elasticsearch` **仅**启动 ES（适合通过 `uv run python -m kb` 在宿主
上运行应用）。缺少 API 密钥只会禁用相应功能（无 LLM → chat/extract/ingest 返回 503；无嵌入密钥
→ 仅 BM25）。

---

## compose 栈 {#compose}

`docker-compose.yml` 定义两个服务。

### `elasticsearch`（容器 `kb-es`）

- 从 `elasticsearch/Dockerfile` 构建 —— 官方 `elasticsearch:8.15.3` 镜像，在构建时安装
  `analysis-ik` 插件。
- `discovery.type=single-node`、`xpack.security.enabled=false`，堆固定为 `-Xms1g -Xmx1g`。
- 发布端口 `9200`；数据在命名卷 `es-data`（`/usr/share/elasticsearch/data`），因此能挺过
  `docker compose down`/重建。
- 健康检查轮询 `_cluster/health` 直到 `green`/`yellow`（5s 间隔，30 次重试）。

### `app`（容器 `kb-app`）

- 从根 `Dockerfile` 构建，带构建参数 `INSTALL_OCR`（默认 `"false"`）。
- `depends_on: elasticsearch` 且 `condition: service_healthy` —— 应用等待 ES。
- 读取 `.env`（可选）获取 API 密钥，并**覆盖** `KB_ES__URL=http://elasticsearch:9200`，使其无论
  `.env` 中的 `KB_ES__URL` 为何都经 compose 网络访问 ES。
- 发布端口 `8000`；`restart: unless-stopped`。
- 绑定挂载（宿主 → 容器）：
  - `./config` → `/app/config` —— 使种子 CSV 编辑**以及**启动时的 taxonomy 自动同步（会重写
    `config/taxonomy.yaml`）持久化到宿主。该目录必须保持可写。
  - `./data/uploads` → `/app/data/uploads` —— 使上传/导入的文件挺过容器重建。
- 健康检查：`curl -fs http://localhost:8000/readyz`（15s 间隔，40s 启动宽限）。

!!! warning "运行时资产按工作目录相对读取"
    镜像设 `WORKDIR /app`，应用相对它读取 `config/`、种子 CSV、`data/uploads` 和
    `Knowledge Base Search.html`。请保持这些路径就位（compose 绑定挂载了前两者）。

---

## 应用镜像 {#image}

`Dockerfile` 是多阶段 `uv` 构建：

- **Builder**（`python:3.12-slim`）—— 从 `uv.lock` 可复现地安装依赖并默认烤入 `ingest` extra
  （PDF/XLSX/PPTX/DOCX 导入开箱即用），随后将项目作为非可编辑 wheel 装入 `/app/.venv`。
- **可选 OCR** —— `--build-arg INSTALL_OCR=true` 追加 `paddleocr` + `paddlepaddle`（~1.5–2 GB）
  以及额外运行时库 `libgl1` / `libglib2.0-0`。默认关闭以保持镜像精简。
- **Runtime**（`python:3.12-slim`）—— 拷入 venv、`config/` 与前端 HTML；以 `tini` 作 PID 1 实现
  干净的信号处理；`HEALTHCHECK` 命中 `/readyz`；入口 `python -m kb --host 0.0.0.0 --port 8000`。

```bash
docker build -t kb-app .                               # 精简（无 OCR）
docker build -t kb-app --build-arg INSTALL_OCR=true .  # 含 PaddleOCR
```

要在 compose 中启用 OCR，在 `app` 服务下设 `args.INSTALL_OCR: "true"` 并重新构建。

---

## 生产加固 {#hardening}

默认 compose 栈是**单节点、关安全**的开发配置。任何超出本地用途的场景：

| 关注点 | 做法 | 设置 / 机制 |
|---|---|---|
| **ES over TLS** | 指向 HTTPS ES 并校验证书 | `KB_ES__URL=https://…`、`KB_ES__VERIFY_CERTS=true`；自签节点设 `KB_ES__SSL_FINGERPRINT`（ES 启动输出的 SHA-256） |
| **ES 鉴权** | 启用 `xpack.security`，创建最小权限用户 | `KB_ES__USERNAME` / `KB_ES__PASSWORD` |
| **密钥** | 切勿烤进镜像 | 通过 `.env` 挂载，或经编排器密钥库注入 `KB_LLM__API_KEY` / `KB_EMBEDDING__API_KEY` |
| **认证/授权** | 应用本身没有 —— 放在网关后 | 由反向代理/API 网关执行鉴权（见[安全](security.zh.md)） |
| **堆 / 资源** | 1 GB 堆是开发尺寸 | 调大 `ES_JAVA_OPTS`；按语料规模调整 ES |
| **扩展** | 应用除内存中的导入会话外无状态 | 多副本置于负载均衡之后；注意导入会话预览状态是每进程的（见下） |

!!! note "导入会话在进程内"
    暂存导入会话存于应用进程内存中（按 TTL 回收）。在负载均衡之后，请将导入审核流程绑定到单一副本
    （粘性会话），或为 ingest UI 将应用缩为单副本 —— 已提交文档与文件追踪在 ES 中共享，仅*预览*
    状态是本地的。

---

## 数据持久化与备份 {#backup}

三类状态，均可恢复：

1. **ES 中的搜索文档** —— *导入*文档的事实来源是 `kb_import_files` 追踪索引加种子 CSV。每次启动应用
   都会从 CSV **清空并重新播种**主索引，随后 `restore_imports()` 从追踪索引重放已提交导入（见
   [从零搭建 → 启动](../reference/build-from-scratch.zh.md#startup)）。
2. **种子 CSV + `config/`** —— 纳入版本控制；它们完整定义被播种的语料。
3. **`es-data` 卷** —— 若想无需重播即可时点恢复，则备份它：

```bash
# 将 ES 数据卷快照为 tar 包
docker run --rm -v knowledgebase_es-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/es-data-$(date +%F).tgz -C /data .

# 恢复到全新卷
docker run --rm -v knowledgebase_es-data:/data -v "$PWD":/backup alpine \
  sh -c "cd /data && tar xzf /backup/es-data-2026-06-05.tgz"
```

更大规模的部署建议用 Elasticsearch 的
[快照 API](https://www.elastic.co/guide/en/elasticsearch/reference/current/snapshot-restore.html)
对接共享仓库，而非裸卷拷贝。

---

## 文档自动发布 {#docs-publish}

文档站点（即本站）通过**本地 `pre-push` git 钩子**发布到 GitHub Pages —— 刻意不用 GitHub Actions
以规避 CI 成本。

- 钩子在 `scripts/git-hooks/` 中纳入版本控制，由 `./scripts/install-hooks.sh` 激活（设置
  `core.hooksPath`，这也是该目录同时携带仓库 Git LFS 透传钩子 `post-checkout`、`post-commit`、
  `post-merge` 的原因）。
- 当推送到 `main` 且触及 `docs/` 或 `mkdocs.yml` 时，`pre-push` 钩子运行 `scripts/deploy-docs.sh`，
  执行 `mkdocs gh-deploy --strict`（构建 + 推送到 `gh-pages`）。strict 构建失败会**中止推送**。
- 直接运行 `scripts/deploy-docs.sh` 可按需发布；用 `git push --no-verify` 可一次性绕过钩子。

请先在本地构建/预览：

```bash
uv sync --extra docs
uv run mkdocs serve              # 在 :8000 预览 EN/中文
uv run mkdocs build --strict     # 链接/锚点损坏即失败；输出 → ./site（git 忽略）
```
