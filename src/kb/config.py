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
    model: str = "BAAI/bge-m3"
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


class TaxonomyConfig(BaseModel):
    path: str = "config/taxonomy.yaml"


class LLMConfig(BaseModel):
    api_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    api_key: str = ""
    model: str = "qwen-plus"
    max_tokens: int = 1200


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)


class IngestConfig(BaseModel):
    upload_dir: str = "data/uploads"
    max_file_size_mb: int = Field(default=50, ge=1, le=500)
    allowed_extensions: list[str] = Field(
        default=["pdf", "xlsx", "xls", "csv", "pptx", "docx"]
    )
    ocr_enabled: bool = True
    ocr_lang: str = "ch"
    segmentation_max_tokens: int = 4000
    # Characters per LLM chunk. Larger = fewer API calls but more tokens per call.
    # 12000 chars ≈ 3000–4000 tokens of input; fits 6–10 alarm entries comfortably.
    segmentation_chunk_chars: int = Field(default=12000, ge=1000, le=100000)
    session_ttl_minutes: int = Field(default=120, ge=10, le=1440)


class Settings(BaseSettings):
    es: ESConfig = Field(default_factory=ESConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    taxonomy: TaxonomyConfig = Field(default_factory=TaxonomyConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)

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
