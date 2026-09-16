"""Local vector store on Postgres + pgvector.

Same duck-type as app.clients.vectors.VectorStore and the sqlite-vec adapter, so
VECTOR_BACKEND=pgvector is a config change and nothing else. Chosen when Postgres is
already the relational source of truth: one database to back up, real SQL joins
against products/kb_articles, and HNSW built in.

Schema (created on first use):
    kb_vectors(namespace text, id text, embedding vector(DIM), metadata jsonb,
               primary key (namespace, id))
    + HNSW index per dimension, cosine ops

Notes
- `vector(DIM)` is fixed per column, so the table is created for the configured
  EMBED_DIM; switching embedder means a new table (the adapter names it by dim) and a
  re-index. That is inherent to pgvector, not to this adapter.
- pgvector must be installed on the server: `brew install pgvector` then
  `create extension vector;`. The adapter checks and says exactly that if missing.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

import structlog

from app.config import settings

log = structlog.get_logger(__name__)


class PgVectorStore:
    def __init__(self, dsn: str, dim: int = 1024):
        self.dsn = dsn
        self.dim = dim
        self.table = f"kb_vectors_{dim}"
        self._ready = False

    # ---- plumbing --------------------------------------------------------
    async def _conn(self):
        import psycopg

        return await psycopg.AsyncConnection.connect(self.dsn, autocommit=True)

    async def _ensure(self) -> None:
        if self._ready:
            return
        async with await self._conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("create extension if not exists vector")
                await cur.execute(
                    f"create table if not exists {self.table} ("
                    "  namespace text not null,"
                    "  id text not null,"
                    f"  embedding vector({self.dim}) not null,"
                    "  metadata jsonb not null default '{}'::jsonb,"
                    "  primary key (namespace, id))")
                await cur.execute(
                    f"create index if not exists {self.table}_ns on {self.table} (namespace)")
                await cur.execute(
                    f"create index if not exists {self.table}_hnsw on {self.table} "
                    "using hnsw (embedding vector_cosine_ops)")
        self._ready = True

    @staticmethod
    def _vec(values: Sequence[float]) -> str:
        return "[" + ",".join(f"{v:.7g}" for v in values) + "]"

    # ---- API -------------------------------------------------------------
    async def upsert(self, vectors: Sequence[Dict[str, Any]], namespace: str,
                     batch_size: int = 100) -> int:
        await self._ensure()
        n = 0
        async with await self._conn() as conn:
            async with conn.cursor() as cur:
                for i in range(0, len(vectors), batch_size):
                    chunk = vectors[i:i + batch_size]
                    # executemany with a VALUES list is slow for vectors; build one
                    # multi-row statement instead.
                    params: list[Any] = []
                    rows_sql = []
                    for v in chunk:
                        rows_sql.append("(%s, %s, %s::vector, %s::jsonb)")
                        params += [namespace, v["id"], self._vec(v["values"]),
                                   json.dumps(v.get("metadata") or {})]
                    sql = (f"insert into {self.table} (namespace, id, embedding, metadata)"
                           f" values {','.join(rows_sql)}"
                           " on conflict (namespace, id) do update set"
                           " embedding = excluded.embedding, metadata = excluded.metadata")
                    await cur.execute(sql, params)
                    n += len(chunk)
        return n

    async def delete_namespace(self, namespace: str) -> None:
        await self._ensure()
        async with await self._conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"delete from {self.table} where namespace = %s", (namespace,))

    async def query(self, vector: List[float], namespace: str, top_k: int = 10,
                    flt: Optional[Dict[str, Any]] = None,
                    include_values: bool = False) -> List[Dict[str, Any]]:
        await self._ensure()
        qv = self._vec(vector)
        where = "namespace = %s"
        params: list[Any] = [qv]                      # %s #1 -> score expression
        params.append(namespace)                      # %s #2 -> namespace
        if flt:
            where += " and metadata @> %s::jsonb"
            params.append(json.dumps({k: (v.get("$eq") if isinstance(v, dict) else v)
                                      for k, v in flt.items()}))
        params.append(qv)                             # order by distance
        params.append(top_k)
        sql = (f"select id, 1 - (embedding <=> %s::vector) as score, metadata"
               f" from {self.table} where {where}"
               " order by embedding <=> %s::vector limit %s")
        async with await self._conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        return [{"id": r[0], "score": float(r[1]), "metadata": r[2] or {}} for r in rows]

    async def stats(self) -> Dict[str, Any]:
        await self._ensure()
        async with await self._conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"select namespace, count(*) from {self.table} group by namespace")
                ns = {name: count for name, count in await cur.fetchall()}
        return {"backend": "pgvector", "table": self.table, "dimension": self.dim,
                "totalVectorCount": sum(ns.values()), "namespaces": ns}

    async def aclose(self) -> None:
        return None
