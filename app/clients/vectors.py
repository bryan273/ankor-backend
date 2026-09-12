"""Pinecone data-plane client, spoken over plain HTTP.

The index (`anker-support`, 3072-d, cosine, serverless aws/us-east-1) already exists.
We use httpx rather than the SDK because everything else in this service is async and
the whole surface we need is three endpoints.

Namespaces isolate corpora so a robot-vacuum manual chunk can never surface in a
breast-pump thread — the single worst failure this system could make.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Sequence

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

NS_PRODUCTS = "products"
NS_KB = "kb"
NS_TICKETS = "tickets"
NS_DEALERS = "dealers"
NS_COMMUNITY = "community"
ALL_NAMESPACES = (NS_PRODUCTS, NS_KB, NS_TICKETS, NS_DEALERS, NS_COMMUNITY)


class VectorStore:
    def __init__(self, api_key: Optional[str] = None, host: Optional[str] = None):
        self.api_key = api_key or settings.pinecone_api_key
        self._host = (host or settings.pinecone_host).replace("https://", "").strip("/")
        self._client: Optional[httpx.AsyncClient] = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self._host:
                self._host = await self._resolve_host()
            self._client = httpx.AsyncClient(
                base_url=f"https://{self._host}",
                headers={"Api-Key": self.api_key, "Content-Type": "application/json",
                         "X-Pinecone-API-Version": "2025-04"},
                timeout=60.0,
            )
        return self._client

    async def _resolve_host(self) -> str:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(
                f"https://api.pinecone.io/indexes/{settings.pinecone_index}",
                headers={"Api-Key": self.api_key, "X-Pinecone-API-Version": "2025-04"},
            )
            r.raise_for_status()
            return r.json()["host"]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def upsert(
        self, vectors: Sequence[Dict[str, Any]], namespace: str, batch_size: int = 100,
    ) -> int:
        """`vectors` are `{"id", "values", "metadata"}`. Pinecone caps a request at
        2 MB / 1000 vectors; 3072 floats is ~40 KB serialised, so 100 is the safe batch."""
        client = await self._http()
        sent = 0
        for i in range(0, len(vectors), batch_size):
            batch = list(vectors[i:i + batch_size])
            for attempt in range(3):
                try:
                    r = await client.post("/vectors/upsert",
                                          json={"vectors": batch, "namespace": namespace})
                    r.raise_for_status()
                    sent += len(batch)
                    break
                except httpx.HTTPError as e:
                    if attempt == 2:
                        raise
                    log.warning("pinecone.upsert_retry", attempt=attempt, error=str(e)[:120])
                    await asyncio.sleep(2 ** attempt)
        return sent

    async def query(
        self, vector: List[float], namespace: str, top_k: int = 10,
        flt: Optional[Dict[str, Any]] = None, include_values: bool = False,
    ) -> List[Dict[str, Any]]:
        client = await self._http()
        payload: Dict[str, Any] = {
            "vector": vector, "topK": top_k, "namespace": namespace,
            "includeMetadata": True, "includeValues": include_values,
        }
        if flt:
            payload["filter"] = flt
        r = await client.post("/query", json=payload)
        r.raise_for_status()
        return r.json().get("matches", [])

    async def delete_namespace(self, namespace: str) -> None:
        client = await self._http()
        r = await client.post("/vectors/delete", json={"deleteAll": True, "namespace": namespace})
        if r.status_code not in (200, 404):
            r.raise_for_status()

    async def stats(self) -> Dict[str, Any]:
        client = await self._http()
        r = await client.post("/describe_index_stats", json={})
        r.raise_for_status()
        return r.json()


_store: Optional[VectorStore] = None


def get_vectors():
    """Backend-agnostic: Pinecone by default, or the local sqlite-vec store when
    VECTOR_BACKEND=sqlite. Callers keep using the VectorStore duck-type."""
    global _store
    if _store is None:
        from app.adapters import get_vector_store

        _store = get_vector_store()
    return _store
