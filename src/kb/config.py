from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class ESConfig(BaseModel):
    url: str = "https://localhost:9200"
    index_prefix: str = "kb"
    request_timeout_s: int = 10
    # Set to the SHA-256 fingerprint from Elasticsearch startup output, or leave
    # empty to use system CA bundle. Set verify_certs=false for local dev only.
    ssl_fingerprint: str | None = None
    verify_certs: bool = True
    username: str | None = None
    password: str | None = None
    # Analyzer names. Defaults use IK (installed via elasticsearch/Dockerfile).
    # Fallback for environments without the plugin: set both to "cjk" (built-in).
    analyzer_index: str = "ik_max_word"
    analyzer_query: str = "ik_smart"


class EmbeddingConfig(BaseModel):
    url: str = "http://localhost:8080"
    api_key: str = ""
    model: str = "text-embedding-v3"
    dims: int = 1024
    # DashScope's OpenAI-compatible embeddings endpoint rejects batches >10.
    batch_size: int = Field(default=10, ge=1, le=128)
    timeout_s: int = 30


class SearchConfig(BaseModel):
    strict_max_hits: int = Field(default=8, ge=1, le=50)
    title_boost: float = Field(default=3.0, ge=1.0, le=10.0)
    # rescore_window: how many top keyword-recall hits get BM25+vector re-ranking.
    rrf_window: int = Field(default=50, ge=10, le=500)
    # Weight of the vector (cosine) score in the BM25+vector ranking blend.
    # Final score = (1 - vector_weight) * BM25 + vector_weight * (cosine_sim + 1)
    vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    # Deepest page reachable via from_+size. Mirrors ES `index.max_result_window`
    # (default 10000). A request past this is rejected at the model with a clear
    # 400 instead of failing inside Elasticsearch with an opaque 502.
    max_result_window: int = Field(default=10000, ge=10, le=100000)


class TaxonomyConfig(BaseModel):
    path: str = "config/taxonomy.yaml"


class LLMConfig(BaseModel):
    api_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    api_key: str = ""
    model: str = "qwen-plus"
    max_tokens: int = 1200
    # Default read-timeout for a chat completion. Segmentation overrides this
    # per-call with a payload-derived timeout (see services/segmentation.py).
    timeout_s: int = Field(default=20, ge=1, le=300)
    # Shorter budget for the structured /extract parameter call.
    extract_timeout_s: int = Field(default=10, ge=1, le=300)
    # Transient-failure retries (429 / 5xx / timeout). 0 disables retrying.
    max_retries: int = Field(default=2, ge=0, le=5)


class ObservabilityConfig(BaseModel):
    # Expose Prometheus metrics at GET /metrics.
    metrics_enabled: bool = True
    # Emit logs as one JSON object per line (machine-readable). When false,
    # logs use a human-readable line that still carries the request id.
    json_logs: bool = False
    log_level: str = "INFO"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)


class IngestConfig(BaseModel):
    upload_dir: str = "data/uploads"
    # Server-side folder scans (POST /ingest/scan) are confined to this root so a
    # caller can't read arbitrary host paths. Folders outside it are rejected.
    scan_root: str = "data"
    max_file_size_mb: int = Field(default=50, ge=1, le=500)
    allowed_extensions: list[str] = Field(
        default=["pdf", "xlsx", "xls", "csv", "pptx", "docx"]
    )
    # Guards against pathological/oversized files exhausting memory during
    # extraction. A document beyond these bounds is rejected with a clear error
    # rather than risking an OOM that takes the whole process down.
    pdf_max_pages: int = Field(default=2000, ge=1, le=50000)
    xlsx_max_cells: int = Field(default=2_000_000, ge=1000)
    ocr_enabled: bool = True
    # PaddleOCR language model. "ch" also reads Latin script (English model
    # numbers/units); use "en" for English-only documents.
    ocr_lang: str = "ch"
    # Drop OCR lines below this recognition confidence so low-quality scans
    # don't feed garbage tokens into the segmentation LLM.
    ocr_min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    segmentation_max_tokens: int = 8000
    # Characters per LLM chunk. Larger = fewer API calls but more tokens per call.
    # 12000 chars ≈ 3000–4000 tokens of input; fits 6–10 alarm entries comfortably.
    segmentation_chunk_chars: int = Field(default=12000, ge=1000, le=100000)
    # Soft TTL: evict COMMITTED/FAILED sessions this long after creation.
    session_ttl_minutes: int = Field(default=120, ge=10, le=1440)
    # Hard TTL: evict *any* session (including in-flight / under-review) this
    # long after creation, bounding memory against abandoned preview sessions.
    session_hard_ttl_minutes: int = Field(default=480, ge=10, le=10080)
    # How often the background sweeper reclaims expired sessions, so an idle
    # server (no new uploads to trigger eviction) doesn't pin abandoned ones.
    session_evict_interval_minutes: int = Field(default=15, ge=1, le=1440)


class Settings(BaseSettings):
    es: ESConfig = Field(default_factory=ESConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    taxonomy: TaxonomyConfig = Field(default_factory=TaxonomyConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="KB_",
        env_nested_delimiter="__",
        extra="ignore",
        yaml_file="config/settings.yaml",
        yaml_file_encoding="utf-8",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence, highest first: init kwargs > shell env > .env > settings.yaml > secrets.
        # settings.yaml ranks *below* env vars so KB_* overrides (e.g. KB_ES__URL in
        # docker-compose) win over the file's defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings — precedence: shell env > .env > config/settings.yaml > defaults."""
    return Settings()
