"""Backend adapters: cloud (Pinecone/Supabase/Gemini) <-> local, switchable by env.

The whole point: any build sitting on this foundation keeps calling
`app.clients.vectors.get_vectors()` and `app.clients.embed.get_embedder()` and never
learns which backend answered. Switching is configuration, not code:

    VECTOR_BACKEND=pinecone | sqlite       (local, no server, one file)
    EMBED_PROVIDER=gemini | openai_compat | hash
    DATABASE_URL=postgresql://...          (cloud pooler or local .localdb, same schema)

Defaults reproduce today's behaviour exactly (pinecone + gemini), so an existing
deployment is unaffected until someone sets the flags.

Add a backend without touching callers: implement the duck-type, register it in the
two factories below, done.
"""
from __future__ import annotations

from typing import Any, Dict

import structlog

from app.config import settings

log = structlog.get_logger(__name__)


def get_vector_store() -> Any:
    backend = (settings.vector_backend or "pinecone").strip().lower()
    if backend in ("pinecone", "cloud"):
        from app.clients.vectors import VectorStore

        return VectorStore()
    if backend in ("sqlite", "sqlite_vec", "local"):
        from app.adapters.local_vector import SqliteVectorStore

        return SqliteVectorStore(path=settings.vector_sqlite_path,
                                 embed_dim=settings.embed_dim)
    raise ValueError(f"unknown VECTOR_BACKEND={backend!r} (pinecone|sqlite)")


def get_embedder() -> Any:
    provider = (settings.embed_provider or "gemini").strip().lower()
    if provider in ("gemini", "cloud"):
        from app.clients.embed import Embedder

        return Embedder()
    if provider in ("openai_compat", "ollama", "local", "vllm", "llamacpp"):
        from app.adapters.local_embed import OpenAICompatEmbedder

        return OpenAICompatEmbedder(
            base_url=settings.embed_base_url,
            model=settings.embed_local_model or settings.embed_model,
            api_key=settings.embed_api_key,
            dim=settings.embed_dim,
        )
    if provider in ("fastembed", "onnx"):
        from app.adapters.local_embed import FastEmbedEmbedder

        return FastEmbedEmbedder(
            model=settings.embed_local_model or "BAAI/bge-small-en-v1.5",
            dim=settings.embed_dim,
        )
    if provider == "hash":
        from app.adapters.local_embed import HashEmbedder

        return HashEmbedder(dim=settings.embed_dim)
    raise ValueError(
            f"unknown EMBED_PROVIDER={provider!r} "
            "(gemini|openai_compat|fastembed|hash)")


def describe_backends() -> Dict[str, Any]:
    """What /healthz and any operator UI should show: never guess which stack is live."""
    return {
        "vector_backend": settings.vector_backend,
        "embed_provider": settings.embed_provider,
        "embed_model": (settings.embed_local_model or settings.embed_model),
        "embed_dim": settings.embed_dim,
        "vector_sqlite_path": settings.vector_sqlite_path,
        "database": "cloud" if "supabase." in (settings.db_url or "") else "local",
    }
