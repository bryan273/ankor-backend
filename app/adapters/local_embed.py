"""Local / self-hosted embedders, same duck-type as `app.clients.embed.Embedder`:

    embed_one(text, task_type) -> [float]
    embed_many(texts, task_type, concurrency) -> [[float]]
    embed_query(text) -> [float]
    aclose()

Providers (EMBED_PROVIDER):
  openai_compat - any OpenAI-shaped /v1/embeddings server. This is the one that lets
                  a laptop serve embeddings with no Google quota: Ollama
                  (nomic-embed-text / bge-m3), llama.cpp --embedding, vLLM, LMX/MLX.
  hash          - deterministic pseudo-embedding, TEST WIRING ONLY. It is not a
                  semantic model; it exists so the adapter can be exercised offline
                  without spending a single Google API call.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import random
from typing import List, Optional, Sequence

import httpx
import structlog

log = structlog.get_logger(__name__)


class LocalEmbedError(RuntimeError):
    pass


class OpenAICompatEmbedder:
    """Talks to Ollama / llama.cpp / vLLM / any OpenAI-compatible embeddings server."""

    def __init__(self, base_url: str, model: str, api_key: str = "", dim: int = 3072,
                 timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.dim = dim
        self._client: Optional[httpx.AsyncClient] = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                            timeout=120.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def embed_one(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        out = await self.embed_many([text], task_type, concurrency=1)
        return out[0]

    async def embed_many(self, texts: Sequence[str], task_type: str = "RETRIEVAL_DOCUMENT",
                         concurrency: int = 4) -> List[List[float]]:
        client = await self._http()
        sem = asyncio.Semaphore(concurrency)

        async def one(t: str) -> List[float]:
            async with sem:
                for attempt in range(3):
                    try:
                        r = await client.post("/embeddings",
                                              json={"model": self.model, "input": t[:8000]})
                        if r.status_code == 429 or r.status_code >= 500:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        r.raise_for_status()
                        return r.json()["data"][0]["embedding"]
                    except httpx.HTTPError as e:
                        if attempt == 2:
                            raise LocalEmbedError(f"local embed failed: {e}") from e
                        await asyncio.sleep(2 ** attempt)
                raise LocalEmbedError("local embed failed after retries")

        return await asyncio.gather(*(one(t) for t in texts))

    async def embed_query(self, text: str) -> List[float]:
        return await self.embed_one(text, "RETRIEVAL_QUERY")


class HashEmbedder:
    """Deterministic pseudo-embedder. TEST WIRING ONLY - carries no meaning.

    Used by scripts/parity_check.py and the adapter tests so both backends can be
    driven with identical vectors without touching Gemini (whose quota is a hard
    constraint, see the project's Google rate-limit rule).
    """

    def __init__(self, dim: int = 3072, model: str = "hash-test-only"):
        self.dim = dim
        self.model = model

    def _vec(self, text: str) -> List[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
        rnd = random.Random(seed)
        v = [rnd.uniform(-1, 1) for _ in range(self.dim)]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    async def embed_one(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        return self._vec(text)

    async def embed_many(self, texts: Sequence[str], task_type: str = "RETRIEVAL_DOCUMENT",
                         concurrency: int = 8) -> List[List[float]]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> List[float]:
        return self._vec(text)

    async def aclose(self) -> None:
        return None


class FastEmbedEmbedder:
    """ONNX embedder that runs on the laptop with no torch and no server.

    Chosen over sentence-transformers for one practical reason: torch + deps is
    ~2.5 GB, while fastembed pulls onnxruntime + tokenizers (~200 MB) and the model
    itself is 130-500 MB. On a machine with little disk headroom that difference is
    the whole point. Default model is 384-d, so set EMBED_DIM=384 to match.

    First call downloads the model into the local cache (one time).
    """

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5", dim: int = 384):
        self.model_name = model or "BAAI/bge-small-en-v1.5"
        self.dim = dim
        self._model = None

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding  # type: ignore

            self._model = TextEmbedding(model_name=self.model_name)
            self.dim = len(next(iter(self._model.embed(["dim probe"]))))
        return self._model

    async def embed_one(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        return (await self.embed_many([text], task_type, concurrency=1))[0]

    async def embed_many(self, texts: Sequence[str], task_type: str = "RETRIEVAL_DOCUMENT",
                         concurrency: int = 4) -> List[List[float]]:
        model = self._load()
        # ONNX inference is CPU-bound; run it off the event loop so a Colab/async
        # caller does not stall, and keep one batched pass rather than N calls.
        loop = asyncio.get_running_loop()
        vecs = await loop.run_in_executor(
            None, lambda: [list(map(float, v)) for v in model.embed(list(texts))])
        return vecs

    async def embed_query(self, text: str) -> List[float]:
        return await self.embed_one(text, "RETRIEVAL_QUERY")

    async def aclose(self) -> None:
        self._model = None
