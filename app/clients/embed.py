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


# `batchEmbedContents` accepts up to 100 contents per request. That ceiling is the
# whole point of using it: 100x fewer requests against a single free-tier key.
BATCH_LIMIT = 100


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

    async def embed_batch(
        self, texts: Sequence[str], task_type: TaskType = "RETRIEVAL_DOCUMENT",
    ) -> List[List[float]]:
        """One request for up to `BATCH_LIMIT` texts, via `batchEmbedContents`.

        This replaces N concurrent single calls. The old approach was a reasonable
        trade when the corpus was small — per-item errors are genuinely easier to
        handle — but it made the request count equal to the chunk count, and at ~120k
        pending chunks against one free-tier key that is months of traffic. The project
        has already had a Google account banned for exactly that pattern, so request
        count is a safety property here, not a performance one.

        The per-item robustness is kept by falling back: if a batch fails, the caller
        retries those texts one at a time, so a single poisoned input still cannot cost
        the other 99.
        """
        if not self.api_key:
            raise EmbedError("GEMINI_EMBED_API_KEY is not set")
        if not texts:
            return []
        client = await self._http()
        payload = {
            "requests": [
                {"model": f"models/{self.model}",
                 "content": {"parts": [{"text": t[:8000]}]},
                 "taskType": task_type}
                for t in texts
            ]
        }
        async with self._sem:
            for attempt in range(5):
                try:
                    r = await client.post(
                        f"{GOOGLE_BASE}/models/{self.model}:batchEmbedContents",
                        params={"key": self.api_key}, json=payload,
                    )
                    if r.status_code == 429 or r.status_code >= 500:
                        # Exponential, and long. A retry storm against a rate limit is
                        # what gets a key banned; waiting is free by comparison.
                        wait = min(60.0, 4.0 * (2 ** attempt))
                        log.warning("embed.throttled", status=r.status_code,
                                    sleeping=wait, batch=len(texts))
                        await asyncio.sleep(wait)
                        continue
                    r.raise_for_status()
                    out = [e["values"] for e in r.json().get("embeddings", [])]
                    if len(out) != len(texts):
                        raise EmbedError(
                            f"batch returned {len(out)} vectors for {len(texts)} texts")
                    return out
                except httpx.HTTPError as e:
                    if attempt == 4:
                        raise EmbedError(f"batch embed failed: {e}") from e
                    await asyncio.sleep(min(60.0, 4.0 * (2 ** attempt)))
        raise EmbedError("batch embed failed after retries")

    async def embed_many(
        self, texts: Sequence[str], task_type: TaskType = "RETRIEVAL_DOCUMENT",
        concurrency: int = 8,
    ) -> List[List[float]]:
        """Embed a list, batched, with a per-item fallback.

        `concurrency` is accepted for call-site compatibility and deliberately ignored:
        the point of this change is to stop issuing one request per text.
        """
        out: List[List[float]] = []
        for i in range(0, len(texts), BATCH_LIMIT):
            window = list(texts[i:i + BATCH_LIMIT])
            try:
                out.extend(await self.embed_batch(window, task_type))
            except EmbedError as e:
                # One bad input must not cost the whole window. This is the per-item
                # error handling the old implementation bought with 100x the requests —
                # kept, but paid for only when something actually goes wrong.
                log.warning("embed.batch_fallback", error=str(e)[:160], size=len(window))
                for text in window:
                    out.append(await self.embed_one(text, task_type))
        return out

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
