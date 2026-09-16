#!/usr/bin/env python3
"""Acceptance gate: the local store must answer like the cloud store.

An adapter is only safe if swapping it does not change the answer. This seeds the
SAME vectors into both backends (a throwaway namespace on Pinecone, a scratch
namespace locally), runs the same queries, and reports overlap@k.

Default embedder is `hash` (deterministic, no quota) so this runs offline and
without spending a single Google call - it validates the *plumbing* (ids, metadata,
filters, ordering, scores). Point EMBED_PROVIDER at a real model when you want to
validate *semantics* as well.

    python scripts/parity_check.py --n 200 --top-k 10
Exit code 0 = parity holds (overlap >= --min-overlap), 1 = it does not.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters import get_embedder  # noqa: E402
from app.adapters.local_vector import SqliteVectorStore  # noqa: E402
from app.clients.vectors import VectorStore  # noqa: E402
from app.config import settings  # noqa: E402

NS = "parity_check"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200, help="vectors to seed per backend")
    ap.add_argument("--queries", type=int, default=10)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--min-overlap", type=float, default=0.9)
    a = ap.parse_args()

    emb = get_embedder()
    print(f"embedder={settings.embed_provider} dim={settings.embed_dim} | "
          f"seed={a.n} queries={a.queries} top_k={a.top_k}")

    docs = [f"parity doc {i} about anker device {i * 7 % 97}" for i in range(a.n)]
    vecs = await emb.embed_many(docs, concurrency=4)
    vectors = [{"id": f"d{i}", "values": v, "metadata": {"kind": "doc", "i": i}}
               for i, v in enumerate(vecs)]
    queries = [f"anker device {(i * 13) % 97}" for i in range(a.queries)]
    qvecs = await emb.embed_many(queries, concurrency=4)
    await emb.aclose()

    cloud = VectorStore()
    local = SqliteVectorStore(path=settings.vector_sqlite_path, embed_dim=settings.embed_dim)
    results = {"cloud": [], "local": []}
    try:
        await cloud.delete_namespace(NS)
        await cloud.upsert(vectors, NS)
        await local.delete_namespace(NS)
        await local.upsert(vectors, NS)
        for qv in qvecs:
            c = await cloud.query(qv, NS, top_k=a.top_k)
            l = await local.query(qv, NS, top_k=a.top_k)
            results["cloud"].append([m["id"] for m in c])
            results["local"].append([m["id"] for m in l])
    finally:
        for store in (cloud, local):
            try:
                await store.delete_namespace(NS)
            except Exception:  # noqa: BLE001
                pass
            await store.aclose()

    overlaps = []
    for c, l in zip(results["cloud"], results["local"]):
        denom = max(len(c), 1)
        overlaps.append(len(set(c) & set(l)) / denom)
    mean = sum(overlaps) / max(len(overlaps), 1)
    print(f"overlap@k per query: {[round(o, 2) for o in overlaps]}")
    print(f"mean overlap@k: {mean:.3f} (min required {a.min_overlap})")
    ok = mean >= a.min_overlap
    print("PARITY: PASS" if ok else "PARITY: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
