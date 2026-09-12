"""Settings, read once at import and shared everywhere.

Anything that differs between a laptop and CI lives here, not in the modules.
"""
from __future__ import annotations

import functools
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    # ── this service ──────────────────────────────────────────────────────────
    app_name: str = "anker-care-agent"
    version: str = "0.1.0"
    backend_api_key: str = Field(default="dev-key", alias="BACKEND_API_KEY")
    cors_origins: str = Field(default="http://localhost:3000", alias="CORS_ORIGINS")
    max_react_iterations: int = Field(default=6, alias="MAX_REACT_ITERATIONS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ── LLM (RKAPI, OpenAI-compatible) ────────────────────────────────────────
    rkapi_base_url: str = Field(default="https://cdn.rkapi.com/v1", alias="RKAPI_BASE_URL")
    rkapi_openai_keys: str = Field(default="", alias="RKAPI_OPENAI_KEYS")
    rkapi_model: str = Field(default="gpt-5.6-terra", alias="RKAPI_MODEL")
    rkapi_timeout: float = Field(default=300.0, alias="RKAPI_TIMEOUT")
    # Measured: one key served 8 concurrent small calls at 2.16/s with no errors.
    # 6 leaves headroom under that while still being ~6x the old serialising limit.
    rkapi_per_key_concurrency: int = Field(default=6, alias="RKAPI_PER_KEY_CONCURRENCY")
    # In-flight turns before the API sheds load with 429 BUSY. A six-minute queue is
    # a worse answer than an honest "we are busy, retry".
    max_concurrent_turns: int = Field(default=24, alias="MAX_CONCURRENT_TURNS")

    # ── text model (DeepSeek; measured ~3x faster than terra on this workload) ─
    # Routed by capability, not preference: DeepSeek has no vision, so images still go
    # to terra. `text_provider=rkapi` puts everything back on one model.
    text_provider: str = Field(default="deepseek", alias="TEXT_PROVIDER")
    # `deepseek-flash` reads images as well as text, so vision no longer needs a second
    # provider. Set to "rkapi" to fail over to gpt-5.6-terra without a code change.
    vision_provider: str = Field(default="deepseek", alias="VISION_PROVIDER")
    deepseek_api_keys: str = Field(default="", alias="DEEPSEEK_API_KEY")
    # Two IDs, ONE underlying model, two serving modes — and the difference is the
    # whole latency budget. `deepseek-chat` answers directly; `deepseek-flash` thinks
    # first and bills the thinking as output tokens. Measured on the same prompt:
    # chat = 0 reasoning tokens / 45 completion / 1.26s, flash = 60 / 123 / 1.91s. Across
    # the 6-8 model calls in one turn that is p50 8.8s vs 16.7s, so text stays on the
    # non-reasoning mode and only vision pays for thinking.
    # `deepseek-chat` is an unlisted legacy alias (the /models endpoint returns only
    # flash and v4-pro) — if it ever stops resolving, set DEEPSEEK_MODEL=deepseek-flash
    # and accept the latency.
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")
    deepseek_vision_model: str = Field(default="deepseek-flash",
                                       alias="DEEPSEEK_VISION_MODEL")
    deepseek_base_url: str = Field(default="https://api.deepseek.com",
                                   alias="DEEPSEEK_BASE_URL")

    # ── embeddings (Google AI Studio direct; RKAPI tokens are chat-only) ──────
    gemini_embed_api_key: str = Field(default="", alias="GEMINI_EMBED_API_KEY")
    embed_model: str = Field(default="gemini-embedding-001", alias="EMBED_MODEL")
    embed_dim: int = Field(default=3072, alias="EMBED_DIM")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")

    # ── vectors ───────────────────────────────────────────────────────────────
    pinecone_api_key: str = Field(default="", alias="PINECONE_API_KEY")
    pinecone_index: str = Field(default="anker-support", alias="PINECONE_INDEX")
    pinecone_host: str = Field(default="", alias="PINECONE_HOST")

    # ── Supabase ──────────────────────────────────────────────────────────────
    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_ref: str = Field(default="", alias="SUPABASE_REF")
    supabase_secret_key: str = Field(default="", alias="SUPABASE_SECRET_KEY")
    supabase_db_password: str = Field(default="", alias="SUPABASE_DB_PASSWORD")
    supabase_region: str = Field(default="ap-northeast-1", alias="SUPABASE_REGION")
    # Optional: point the pool at any Postgres (local dev, tests).
    database_url: str = Field(default="", alias="DATABASE_URL")

    # ── backend adapters: cloud <-> local, same interfaces (app/adapters) ────────
    # Defaults reproduce the hosted stack; set these to run entirely on a laptop.
    vector_backend: str = Field(default="pinecone", alias="VECTOR_BACKEND")
    vector_sqlite_path: str = Field(
        default=".localvec/anki_support.db", alias="VECTOR_SQLITE_PATH")
    embed_provider: str = Field(default="gemini", alias="EMBED_PROVIDER")
    embed_base_url: str = Field(
        default="http://127.0.0.1:11434/v1", alias="EMBED_BASE_URL")
    embed_api_key: str = Field(default="", alias="EMBED_API_KEY")
    embed_local_model: str = Field(default="", alias="EMBED_LOCAL_MODEL")

    # ── optional ──────────────────────────────────────────────────────────────
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    deepgram_api_key: str = Field(default="", alias="DEEPGRAM_API_KEY")
    voice_enabled: bool = Field(default=False, alias="VOICE_ENABLED")
    crawl_user_agent: str = Field(
        default="AnkerHackathonBot/0.1 (+https://github.com/tkc88888888/anker-hackathon-backend)",
        alias="CRAWL_USER_AGENT",
    )

    @field_validator("rkapi_base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def rkapi_keys(self) -> List[str]:
        """Each key is an independent rate lane. Concurrency within a lane is bounded by
        `rkapi_per_key_concurrency`, so total in-flight model calls is keys x that."""
        return [k.strip() for k in self.rkapi_openai_keys.split(",") if k.strip()]

    @property
    def cors_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def db_url(self) -> Optional[str]:
        """Session-mode pooler. The direct `db.<ref>.supabase.co` host is IPv6-only
        on this project and unreachable from most networks — measured, not assumed.

        DATABASE_URL overrides everything, so the same code runs against a local
        Postgres for development and tests without touching the Supabase project.
        """
        if self.database_url.strip():
            return self.database_url.strip()
        if not (self.supabase_ref and self.supabase_db_password):
            return None
        from urllib.parse import quote

        pw = quote(self.supabase_db_password, safe="")
        return (
            f"postgresql://postgres.{self.supabase_ref}:{pw}"
            f"@aws-0-{self.supabase_region}.pooler.supabase.com:5432/postgres?sslmode=require"
        )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
