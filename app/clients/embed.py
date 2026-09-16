"""Embeddings — Google AI Studio direct.

RKAPI tokens are chat-only (every embedding model returns 403 on them, verified), so
this is the one place that talks to Google. OpenAI `text-embedding-3-large` is the
fallback: also 3072-d, so the Pinecone index shape is unchanged if we swap.

Task types matter for retrieval quality on this model: documents are embedded with
`RETRIEVAL_DOCUMENT` and queries with `RETRIEVAL_QUERY`, which places them in the same
space asymmetrically the way the model was trained.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import List, Literal, Optional, Sequence

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

GOOGLE_BASE = "https://generativelanguage.googleapis.com/v1beta"
TaskType = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY", "SEMANTIC_SIMILARITY"]

# Cost, real money not credits: $0.15 per 1M input tokens.
EMBED_PRICE_PER_TOKEN = 0.15 / 1_000_000


def text_hash(text: str) -> str:
    """Content hash so a re-crawl only pays to embed what actually changed."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


class EmbedError(RuntimeError):
    pass


class Embedder:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.gemini_embed_api_key
        self.model = model or settings.embed_model
        self.dim = settings.embed_dim
        self._client: Optional[httpx.AsyncClient] = None
        self._sem = asyncio.Semaphore(8)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def embed_one(self, text: str, task_type: TaskType = "RETRIEVAL_DOCUMENT") -> List[float]:
        if not self.api_key:
            raise EmbedError("GEMINI_EMBED_API_KEY is not set")
        client = await self._http()
        payload = {
            "model": f"models/{self.model}",
            "content": {"parts": [{"text": text[:8000]}]},
            "taskType": task_type,
        }
        async with self._sem:
            for attempt in range(3):
                try:
                    r = await client.post(
                        f"{GOOGLE_BASE}/models/{self.model}:embedContent",
                        params={"key": self.api_key}, json=payload,
                    )
                    if r.status_code == 429 or r.status_code >= 500:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    r.raise_for_status()
                    return r.json()["embedding"]["values"]
                except httpx.HTTPError as e:
                    if attempt == 2:
                        raise EmbedError(f"embed failed: {e}") from e
                    await asyncio.sleep(2 ** attempt)
        raise EmbedError("embed failed after retries")

    async def embed_many(
        self, texts: Sequence[str], task_type: TaskType = "RETRIEVAL_DOCUMENT",
        concurrency: int = 8,
    ) -> List[List[float]]:
        """Concurrent single-input calls. `gemini-embedding-001` does expose a batch
        endpoint, but the per-item error handling of N small calls is worth more than
        the round-trip saving at our corpus size, and one poisoned item cannot fail
        the whole batch."""
        sem = asyncio.Semaphore(concurrency)

        async def one(t: str) -> List[float]:
            async with sem:
                return await self.embed_one(t, task_type)

        return await asyncio.gather(*(one(t) for t in texts))

    async def embed_query(self, text: str) -> List[float]:
        return await self.embed_one(text, "RETRIEVAL_QUERY")


_embedder: Optional[Embedder] = None


def get_embedder():
    """Backend-agnostic: Gemini by default, or a local OpenAI-compatible embedder when
    EMBED_PROVIDER=openai_compat (Ollama / llama.cpp / vLLM / MLX)."""
    global _embedder
    if _embedder is None:
        from app.adapters import get_embedder as _pick

        _embedder = _pick()
    return _embedder
