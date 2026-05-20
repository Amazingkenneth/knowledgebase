from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Analyzer names. Default "cjk" is built-in (no plugin needed).
    # For better Chinese tokenization install analysis-ik and use:
    #   analyzer_index: "ik_max_word"   analyzer_query: "ik_smart"
    analyzer_index: str = "cjk"
    analyzer_query: str = "cjk"


class EmbeddingConfig(BaseModel):
    url: str = "http://localhost:8080"
    model: str = "BAAI/bge-m3"
    dims: int = 1024
    batch_size: int = Field(default=32, ge=1, le=128)
    timeout_s: int = 30


class SearchConfig(BaseModel):
    strict_max_hits: int = Field(default=8, ge=1, le=50)
    title_boost: float = Field(default=3.0, ge=1.0, le=10.0)
    rrf_window: int = Field(default=50, ge=10, le=500)
    rrf_rank_constant: int = Field(default=60, ge=1, le=200)


class TaxonomyConfig(BaseModel):
    path: str = "config/taxonomy.yaml"


class LLMConfig(BaseModel):
    api_url: str = "https://api.deepseek.com/v1/chat/completions"
    api_key: str = ""
    model: str = "deepseek-chat"
    max_tokens: int = 1200


class Settings(BaseSettings):
    es: ESConfig = Field(default_factory=ESConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    taxonomy: TaxonomyConfig = Field(default_factory=TaxonomyConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)

    model_config = SettingsConfigDict(
        env_prefix="KB_",
        env_nested_delimiter="__",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings(settings_path: str | Path = "config/settings.yaml") -> Settings:
    """Load settings.yaml, then layer env-var overrides on top."""
    path = Path(settings_path)
    base: dict[str, object] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path}: top-level YAML must be a mapping")
        base = loaded
    return Settings(**base)  # type: ignore[arg-type]
