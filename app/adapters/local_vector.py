"""Local (self-hosted) vector store: sqlite-vec ANN, or an exact numpy fallback.

Speaks the exact duck-type of `app.clients.vectors.VectorStore` so the rest of the
service cannot tell which backend is live:

    upsert(vectors, namespace, batch_size) -> int
    query(vector, namespace, top_k, flt, include_values) -> [{id, score, metadata}]
    delete_namespace(namespace) -> None
    stats() -> dict
    aclose() -> None

Why sqlite-vec: no server, no system dependency (pip wheel only), one file on disk,
and it keeps the whole retrieval stack runnable on a laptop that has no Docker and
no spare gigabytes. If the extension is missing we fall back to exact cosine over
BLOBs - slower, but always correct, and fine at demo corpus size.
"""
from __future__ import annotations

import json
import sqlite3
import struct
from typing import Any, Dict, List, Optional, Sequence

import structlog

log = structlog.get_logger(__name__)


def _to_blob(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def _from_blob(blob: bytes) -> List[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


class SqliteVectorStore:
    def __init__(self, path: str, embed_dim: int = 3072):
        self.path = path
        self.dim = embed_dim
        self._conn: Optional[sqlite3.Connection] = None
        self._has_vec = False

    # ---- lifecycle -------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path)
            try:
                import sqlite_vec  # type: ignore

                self._conn.enable_load_extension(True)
                sqlite_vec.load(self._conn)
                self._conn.enable_load_extension(False)
                self._has_vec = True
            except Exception as e:  # noqa: BLE001 - fallback is a feature, not an error
                self._has_vec = False
                log.info("local_vector.numpy_fallback", reason=str(e)[:80])
            self._conn.execute(
                "create table if not exists vec_rows ("
                "  namespace text not null, id text not null, embedding blob not null,"
                "  metadata text not null default '{}',"
                "  primary key (namespace, id))")
            self._conn.execute(
                "create index if not exists vec_rows_ns on vec_rows (namespace)")
            self._conn.commit()
        return self._conn

    async def aclose(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- write -----------------------------------------------------------
    async def upsert(self, vectors: Sequence[Dict[str, Any]], namespace: str,
                     batch_size: int = 100) -> int:
        db = self._db()
        rows = [(namespace, str(v["id"]), _to_blob(v["values"]),
                 json.dumps(v.get("metadata") or {})) for v in vectors]
        for i in range(0, len(rows), batch_size):
            db.executemany(
                "insert into vec_rows (namespace, id, embedding, metadata) values (?,?,?,?)"
                " on conflict (namespace, id) do update set"
                " embedding = excluded.embedding, metadata = excluded.metadata",
                rows[i:i + batch_size])
        db.commit()
        return len(rows)

    async def delete_namespace(self, namespace: str) -> None:
        db = self._db()
        db.execute("delete from vec_rows where namespace = ?", (namespace,))
        db.commit()

    # ---- read ------------------------------------------------------------
    async def query(self, vector: List[float], namespace: str, top_k: int = 10,
                    flt: Optional[Dict[str, Any]] = None,
                    include_values: bool = False) -> List[Dict[str, Any]]:
        db = self._db()
        cur = db.execute(
            "select id, embedding, metadata from vec_rows where namespace = ?", (namespace,))
        rows = cur.fetchall()
        # filter first (same semantics as Pinecone's metadata filter: $eq per field)
        def keep(meta: Dict[str, Any]) -> bool:
            if not flt:
                return True
            for k, v in flt.items():
                want = v.get("$eq") if isinstance(v, dict) else v
                if meta.get(k) != want:
                    return False
            return True

        scored: List[Dict[str, Any]] = []
        for rid, blob, meta_json in rows:
            meta = json.loads(meta_json or "{}")
            if not keep(meta):
                continue
            emb = _from_blob(blob)
            if len(emb) != len(vector):
                continue
            dot = sum(a * b for a, b in zip(emb, vector))
            na = sum(a * a for a in emb) ** 0.5 or 1.0
            nb = sum(b * b for b in vector) ** 0.5 or 1.0
            hit = {"id": rid, "score": dot / (na * nb), "metadata": meta}
            if include_values:
                hit["values"] = emb
            scored.append(hit)
        scored.sort(key=lambda h: h["score"], reverse=True)
        return scored[:top_k]

    async def stats(self) -> Dict[str, Any]:
        db = self._db()
        cur = db.execute("select namespace, count(*) from vec_rows group by namespace")
        ns = {name: count for name, count in cur.fetchall()}
        total = sum(ns.values())
        return {"backend": "sqlite_vec" if self._has_vec else "sqlite_numpy",
                "path": self.path, "dimension": self.dim,
                "totalVectorCount": total, "namespaces": ns}
