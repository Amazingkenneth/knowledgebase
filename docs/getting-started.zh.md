# 快速开始

有两种运行方式——按你的目标选择对应行。

| 目标 | 你需要 |
|------|---------------|
| **只想运行**（部署 / 试用） | Docker + Docker Compose 24+ —— 见 [方式 A](#a-docker-compose)。其余不需要。 |
| **开发 / 修改代码** | Python 3.12+、[uv](https://docs.astral.sh/uv/) 与 Docker（用于 Elasticsearch）—— 见 [方式 B](#b-python-es)。 |

LLM 密钥（`KB_LLM__API_KEY`）与 embedding 密钥（`KB_EMBEDDING__API_KEY`）均为**可选**——服务在缺失时也能启动并优雅降级（仅关键词检索，AI 对话关闭）。见[配置](configuration.zh.md)。

---

## 方式 A — Docker Compose（推荐）

全部在容器内运行，唯一依赖是 Docker。

```bash
# 1. 克隆后（可选）填入 API 密钥
cp .env.example .env          # 编辑 .env 设置 KB_LLM__API_KEY / KB_EMBEDDING__API_KEY
                              # （跳过也能运行——仅关键词检索，AI 对话关闭）

# 2. 构建并启动整套服务（ES + IK 插件 + API）
docker compose up -d --build  # 首次构建约 2-3 分钟；后续启动很快
```

打开 **http://localhost:8000**。

| URL | 说明 |
|-----|-------------|
| `http://localhost:8000` | 知识库搜索 UI |
| `http://localhost:8000/docs` | Swagger UI / 交互式 API 文档 |
| `http://localhost:8000/redoc` | ReDoc API 参考 |
| `http://localhost:9200` | Elasticsearch（直连） |

**常用命令：**

```bash
docker compose logs -f app        # 跟踪 API 日志
docker compose ps                 # 服务状态 + 健康检查
docker compose restart app        # 重启 API（如编辑 config/*.csv 后）
docker compose down               # 停止全部（保留 ES 数据与上传）
docker compose down -v            # 停止并清空 Elasticsearch 数据卷
```

Compose 栈提供：

- `app` 服务在 Elasticsearch **健康**后才启动，并通过内部网络访问（自动设置 `KB_ES__URL=http://elasticsearch:9200`，无需手动配置）。
- **持久化**：ES 数据存于命名卷 `es-data`；上传/导入文件存于 `./data/uploads`；CSV 与 `taxonomy.yaml` 从 `./config` 绑定挂载，宿主机改动在下次 `docker compose restart app` 生效。
- **API 密钥**从 `.env` 读取（可选）。`.env` 中的任何内容都会传入容器。

!!! tip "启用 OCR（扫描版 PDF / 图片）"
    OCR（PaddleOCR）默认未打入镜像以保持精简（约 440 MB）。如需内置，在 `docker-compose.yml` 设置构建参数：

    ```yaml
    # docker-compose.yml → services.app.build.args
    INSTALL_OCR: "true"      # 增加约 1.5-2 GB；模型在首次使用时下载
    ```

    然后重建：`docker compose build app && docker compose up -d`。

---

## 方式 B — 本地（宿主 Python + ES 容器）

从源码运行应用以便开发；Elasticsearch 跑在容器里。

```bash
# 1. 仅启动 Elasticsearch（带 IK 分词插件）
docker compose up -d --build elasticsearch

# 2. 安装依赖并以自动重载启动开发服务器
uv run python -m kb --reload          # 端口取自 KB_SERVER__PORT（默认 8000）
uv run python -m kb --port 8001 --reload   # 显式覆盖端口
```

常用开发命令：

```bash
uv run pytest tests/unit                       # 单元测试（无需基础设施）
uv run --extra ingest pytest tests/unit        # 含 PPTX/PDF 抽取测试
uv run --extra ingest --extra ocr pytest tests/unit  # 含 OCR（需要 libGL）
uv run pytest tests/integration -m integration # 需要 Docker
uv run ruff check src tests                    # 代码风格检查
uv run mypy src                                # 类型检查
```

!!! note "测试夹具是生成的，不入库"
    PPTX/PDF 夹具文件由 `tests/unit/conftest.py` 从 seed CSV 即时生成——仓库中不存放大型二进制测试资源。

---

## 种子数据与数据加载

每次启动时，服务都会**清空每个索引的全部文档并从 `config/` 下的 CSV 重新加载**。CSV 的新增、修改、删除行都会在下次重启生效。seed 之后，`restore_imports()` 会从 `kb_import_files` 追踪索引重新索引此前导入的文档——因此导入文件能在重 seed 后幸存。见[架构 → 文件导入管道](architecture/import-pipeline.zh.md#file-tracker-kb_import_files)。

---

## 在本地阅读本文档

本文档站点由 [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) 构建。要带实时刷新地预览，或发布它：

```bash
uv sync --extra docs           # 安装 mkdocs-material + i18n 插件
uv run mkdocs serve            # 在 http://127.0.0.1:8000 实时预览
uv run mkdocs build --strict   # 构建静态站点到 ./site（链接断裂即失败）
uv run mkdocs gh-deploy        # 发布到 gh-pages 分支（GitHub Pages）
```

站点为中英双语，使用页头的语言切换器即可。
