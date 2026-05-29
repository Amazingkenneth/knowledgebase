# syntax=docker/dockerfile:1
#
# App image for the Knowledge Base API.
# Elasticsearch has its own image (elasticsearch/Dockerfile); this builds the
# FastAPI service and is wired into docker-compose.yml as the `app` service.
#
# Build slim (default):   docker build -t kb-app .
# Build with OCR:         docker build -t kb-app --build-arg INSTALL_OCR=true .

############################
# Builder — resolve deps into /app/.venv with uv (reproducible from uv.lock)
############################
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies first (cached layer) — includes the `ingest` extra so
# PDF/XLSX/PPTX/DOCX import works out of the box.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra ingest

# Install the project itself (non-editable wheel into the venv).
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --extra ingest

# Optional OCR — heavy (~1.5-2GB) and not part of pyproject. Off by default;
# enables the PaddleOCR fallback for scanned PDFs/images (src/kb/services/ocr.py).
ARG INSTALL_OCR=false
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_OCR" = "true" ]; then \
        uv pip install --python /app/.venv/bin/python paddleocr paddlepaddle numpy ; \
    fi

############################
# Runtime
############################
FROM python:3.12-slim AS runtime

# curl: container healthcheck. tini: clean PID-1 signal handling. libgomp1: numpy.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl tini libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# When OCR is baked in, opencv/paddle need these extra shared libs (~150MB).
# Kept out of the default build so the slim image stays lean.
ARG INSTALL_OCR=false
RUN if [ "$INSTALL_OCR" = "true" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            libgl1 libglib2.0-0 \
        && rm -rf /var/lib/apt/lists/* ; \
    fi

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Virtualenv (with the kb package + deps) from the builder.
COPY --from=builder /app/.venv /app/.venv

# Assets the app reads relative to the working directory at runtime.
# In docker-compose these are bind-mounted so edits + taxonomy auto-sync persist
# to the host; the copies here keep a plain `docker run` self-contained.
COPY config ./config
COPY ["Knowledge Base Search.html", "./"]

# Upload target — overridden by a bind mount/volume in compose.
RUN mkdir -p data/uploads

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fs http://localhost:8000/healthz || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "kb", "--host", "0.0.0.0", "--port", "8000"]
