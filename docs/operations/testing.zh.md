# 测试与开发 {#testing}

本页介绍测试套件布局、各层级的运行方式、让仓库免于二进制资产的 fixture 即时生成技巧，以及新增
知识类型时如何扩展测试。工具链为 `uv` + `pytest` + `ruff` + `mypy`，均在 `pyproject.toml` 中声明。

---

## 测试布局 {#layout}

```
tests/
├── unit/            # 无需基础设施 —— 纯逻辑、mock 客户端
│   ├── conftest.py            # 即时生成 PPTX/PDF/XLSX fixture
│   ├── test_search_queries.py # ES 查询体结构
│   ├── test_search_banner.py  # status→banner 契约
│   ├── test_body_builder.py   # 固定的 `body` 文本布局
│   ├── test_document_validation.py
│   ├── test_indexing_validation.py
│   ├── test_extraction.py / test_extraction_limits.py
│   ├── test_segmentation_parse.py / test_segmentation_routing.py
│   ├── test_import_pipeline_logic.py / test_import_files.py
│   ├── test_import_security.py # 上传路径穿越防护
│   ├── test_embedding_client.py / test_llm_client.py
│   ├── test_taxonomy.py / test_spec.py / test_config.py / test_feedback.py
│   └── …
├── api/             # 通过 TestClient 测试 FastAPI 路由（无活动 ES）
│   ├── conftest.py
│   ├── test_routes.py / test_endpoints.py
│   ├── test_input_bounds.py        # 请求大小/边界约束
│   └── test_exception_handlers.py
└── integration/     # 需要 Docker / 活动的 Elasticsearch
    ├── conftest.py
    └── test_indexing_and_search.py
```

按成本分三层：

- **`tests/unit`** —— 无需基础设施。上游客户端（ES、嵌入、LLM）被 mock，或仅在纯解析/校验逻辑上
  运行。快速，随处可跑。
- **`tests/api`** —— 通过 `TestClient` 驱动 FastAPI 应用；仍无活动 ES。
- **`tests/integration`** —— 标记 `@pytest.mark.integration`；需要 Docker（带 IK 插件的真实 ES）。
  `integration` 标记在 `pyproject.toml` 声明（`markers = ["integration: requires Docker / Elasticsearch"]`）。

---

## 运行套件 {#running}

```bash
uv run pytest tests/unit                         # 单元 —— 缺少 extras 时跳过 ingest 测试
uv run --extra ingest pytest tests/unit          # + PPTX/PDF/XLSX 抽取测试
uv run --extra ingest --extra ocr pytest tests/unit  # + OCR 行为（宿主需 libGL）
uv run pytest tests/api                           # 路由级测试
uv run pytest tests/integration -m integration    # 需要 Docker
```

`ingest` extra 安装 `pymupdf` / `openpyxl` / `python-pptx` / `python-docx`；`ocr` extra 追加
`paddleocr` / `paddlepaddle`（较重，且 OCR 测试需宿主存在 `libGL` —— `libgl1`）。不带 `ingest`
extra 时抽取测试被**跳过**而非失败，因此最小安装下 `uv run pytest tests/unit` 仍为绿。

提交前请同时运行 lint 与类型检查：

```bash
uv run ruff check src tests
uv run mypy src
```

---

## Fixture 是生成的，而非提交的 {#fixtures}

`tests/unit/conftest.py` 每个 pytest 会话从 `config/` 下三个种子 CSV **即时构建**最小但内容完整的
PPTX/PDF/XLSX 文件，写入由 pytest 管理的临时目录（自动清理）。这是刻意为之：没有大体积二进制模板
进入 git，生成的文件足够朴素（无图片/主题）以便低成本地走通文本抽取代码路径。CSV 读取器依次尝试
`utf-8-sig`、`utf-8`、`gb18030` 编码 —— 与带 BOM 的中文种子文件匹配。

由此带来的好处：当你修改某个种子 CSV 的列时，生成的 fixture 随之改变，抽取测试便自动与真实种子格式
保持同步。

---

## 为新知识类型添加测试 {#new-type}

新增知识类型时（完整步骤见
[数据模型 → 新增类型](../reference/data-model.zh.md)），照搬既有模式：

1. **模型校验** —— 在 `test_document_validation.py` 为新子类添加用例（必填 vs 可选字段、taxonomy
   约束）。
2. **body 布局** —— 扩展 `test_body_builder.py`：`body` 文本拼装顺序由测试*固定*，新类型的分节顺序
   必须断言。
3. **切分** —— 添加路由 + 解析用例（`test_segmentation_routing.py`、`test_segmentation_parse.py`），
   覆盖 LLM 分类器与按类型抽取器。
4. **spec 一致性** —— `test_spec.py` 检查每个 `config/knowledge_types/*.yaml` spec 与其模型一致；
   在此加入新 spec。
5. **种子往返** —— 添加 CSV 表头文件并确认 conftest fixture 生成器能处理它。

新测试尽量放在 `tests/unit`，除非确实需要活动 ES —— 那时标记 `@pytest.mark.integration` 并放到
`tests/integration`。

---

## 本地开发服务器 {#dev-server}

```bash
docker compose up -d --build elasticsearch   # 仅 ES
uv run python -m kb --reload                  # 应用监听 KB_SERVER__PORT（默认 8000）
uv run python -m kb --port 8001 --reload      # 显式端口覆盖
```

`python -m kb` 从 settings 读取端口；`uv run uvicorn kb.main:app --reload` 直接运行应用但**不**从
settings 读取端口。完整本地开发循环见[快速开始](../getting-started.zh.md)，容器路径见
[部署](deployment.zh.md)。
