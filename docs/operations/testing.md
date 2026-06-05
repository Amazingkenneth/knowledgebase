# Testing & Development {#testing}

This page covers the test suite layout, how to run each tier, the fixture-generation
trick that keeps the repo free of binary assets, and how to extend the tests when you add
a knowledge type. Tooling is `uv` + `pytest` + `ruff` + `mypy`, all declared in
`pyproject.toml`.

---

## Test layout {#layout}

```
tests/
├── unit/            # no infrastructure — pure logic, mocked clients
│   ├── conftest.py            # generates PPTX/PDF/XLSX fixtures on the fly
│   ├── test_search_queries.py # ES query-body shape
│   ├── test_search_banner.py  # status→banner contract
│   ├── test_body_builder.py   # the pinned `body` text layout
│   ├── test_document_validation.py
│   ├── test_indexing_validation.py
│   ├── test_extraction.py / test_extraction_limits.py
│   ├── test_segmentation_parse.py / test_segmentation_routing.py
│   ├── test_import_pipeline_logic.py / test_import_files.py
│   ├── test_import_security.py # path-traversal guard on uploads
│   ├── test_embedding_client.py / test_llm_client.py
│   ├── test_taxonomy.py / test_spec.py / test_config.py / test_feedback.py
│   └── …
├── api/             # FastAPI route tests via TestClient (no live ES)
│   ├── conftest.py
│   ├── test_routes.py / test_endpoints.py
│   ├── test_input_bounds.py        # request-size / bound enforcement
│   └── test_exception_handlers.py
└── integration/     # needs Docker / a live Elasticsearch
    ├── conftest.py
    └── test_indexing_and_search.py
```

Three tiers, by cost:

- **`tests/unit`** — no infrastructure. Upstream clients (ES, embeddings, LLM) are mocked
  or exercised on pure parsing/validation logic. Fast; runs anywhere.
- **`tests/api`** — drives the FastAPI app through `TestClient`; still no live ES.
- **`tests/integration`** — marked `@pytest.mark.integration`; requires Docker (a real ES
  with the IK plugin). The `integration` marker is declared in `pyproject.toml`
  (`markers = ["integration: requires Docker / Elasticsearch"]`).

---

## Running the suite {#running}

```bash
uv run pytest tests/unit                         # unit — skips ingest tests if extras absent
uv run --extra ingest pytest tests/unit          # + PPTX/PDF/XLSX extraction tests
uv run --extra ingest --extra ocr pytest tests/unit  # + OCR behaviour (needs libGL on host)
uv run pytest tests/api                           # route-level tests
uv run pytest tests/integration -m integration    # needs Docker
```

The `ingest` extra installs `pymupdf` / `openpyxl` / `python-pptx` / `python-docx`; the
`ocr` extra adds `paddleocr` / `paddlepaddle` (heavy, and the OCR tests need `libGL` —
`libgl1` — present on the host). Without the `ingest` extra the extraction tests are
**skipped**, not failed, so the base `uv run pytest tests/unit` stays green on a minimal
install.

Lint and type-check (run both before committing):

```bash
uv run ruff check src tests
uv run mypy src
```

---

## Fixtures are generated, not committed {#fixtures}

`tests/unit/conftest.py` builds **minimal but content-complete** PPTX/PDF/XLSX files
on the fly from the three seed CSVs in `config/`, once per pytest session, into a
pytest-managed temp dir (auto-cleaned). This is deliberate: no large binary templates ever
land in git, and the generated files are plain (no images/themes) so they exercise the
text-extraction code paths cheaply. The CSV reader tries `utf-8-sig`, `utf-8`, then
`gb18030` encodings — matching the BOM-prefixed Chinese seed files.

Consequence: if you change a seed CSV's columns, the generated fixtures change with it, so
extraction tests stay in sync with the real seed format automatically.

---

## Adding tests for a new knowledge type {#new-type}

When you add a knowledge type (full procedure in
[Data Model → adding a type](../reference/data-model.md)), mirror the existing pattern:

1. **Model validation** — add cases to `test_document_validation.py` for the new subclass
   (required vs optional fields, taxonomy enforcement).
2. **Body layout** — extend `test_body_builder.py`: the `body` text assembly order is
   *pinned* by tests, so a new type's section order must be asserted.
3. **Segmentation** — add routing + parse cases (`test_segmentation_routing.py`,
   `test_segmentation_parse.py`) so the LLM classifier and per-type extractor are covered.
4. **Spec parity** — `test_spec.py` checks each `config/knowledge_types/*.yaml` spec stays
   consistent with its model; add the new spec there.
5. **Seed round-trip** — add a CSV header file and confirm the conftest fixture generator
   handles it.

Keep new tests in `tests/unit` unless they genuinely need a live ES, in which case mark
them `@pytest.mark.integration` and put them under `tests/integration`.

---

## Local dev server {#dev-server}

```bash
docker compose up -d --build elasticsearch   # ES only
uv run python -m kb --reload                  # app on KB_SERVER__PORT (default 8000)
uv run python -m kb --port 8001 --reload      # explicit port override
```

`python -m kb` reads the port from settings; `uv run uvicorn kb.main:app --reload` runs
the app directly but does **not** read the port from settings. See
[Getting Started](../getting-started.md) for the full local-dev loop and
[Deployment](deployment.md) for the container path.
