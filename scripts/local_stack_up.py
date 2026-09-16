#!/usr/bin/env python3
"""Build the LOCAL vector store so the stack runs with no Pinecone (and no Gemini).

Two modes, both quota-free:

  --mode copy  (default) copy existing vectors OUT of Pinecone by id. No embedding
               model, no API quota, byte-identical vectors -> parity is guaranteed.
  --mode embed re-embed the text with whatever EMBED_PROVIDER is configured
               (openai_compat = Ollama/llama.cpp/vLLM/MLX, or hash for wiring tests).

Examples
--------
    # mirror the products + kb namespaces locally, 5000 rows each, no API calls
    python scripts/local_stack_up.py --mode copy --namespace products,kb --limit 5000

    # fully self-hosted embeddings (Ollama serving bge-m3 on :11434)
    EMBED_PROVIDER=openai_compat EMBED_BASE_URL=http://127.0.0.1:11434/v1 \
      EMBED_LOCAL_MODEL=bge-m3 python scripts/local_stack_up.py --mode embed --namespace kb --limit 200
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters import get_embedder, get_vector_store  # noqa: E402
from app.clients import db as dbmod  # noqa: E402
from app.config import settings  # noqa: E402


async def rows_for(namespace: str, limit: int) -> list[dict]:
    """(id, text, metadata) triples straight from the relational source of truth."""
    if namespace == "products":
        sql = ("select id::text as id, coalesce(name,'') as text, "
               "jsonb_build_object('sku', sku, 'brand', brand, 'category', category, "
               "                   'url', url, 'source', 'products') as metadata "
               "from products limit %s")
    elif namespace == "kb":
        sql = ("select kc.pinecone_id as id, kc.text as text, "
               "coalesce(kc.meta, '{}'::jsonb) as metadata "
               "from kb_chunks kc where kc.pinecone_id is not null limit %s")
    elif namespace == "dealers":
        sql = ("select id::text as id, coalesce(name,'') as text, "
               "jsonb_build_object('region', region, 'source', 'dealers') as metadata "
               "from dealers limit %s")
    elif namespace == "tickets":
        sql = ("select id::text as id, coalesce(summary,'') as text, "
               "jsonb_build_object('status', status, 'source', 'tickets') as metadata "
               "from tickets limit %s")
    else:
        raise SystemExit(f"unsupported namespace {namespace!r}")
    return await dbmod.fetch(sql, (limit,))


def copy_from_pinecone(namespace: str, ids: list[str], batch: int = 100) -> list[dict]:
    """Fetch existing vectors out of Pinecone.

    This host serves /vectors/fetch over gRPC only (the JSON data plane our app uses
    implements upsert + query, not fetch), so this needs the official SDK. If you do
    not want that dependency, use --mode embed to rebuild the vectors locally instead.
    """
    try:
        from pinecone import Pinecone  # type: ignore
    except ImportError as e:  # noqa: BLE001
        raise SystemExit(
            "--mode copy needs the gRPC client:\n"
            "    .venv/bin/pip install pinecone\n"
            "  (Pinecone serves /vectors/fetch over gRPC only here; upsert+query are JSON.)\n"
            "  Or rebuild vectors locally with:  --mode embed") from e

    pc = Pinecone(api_key=settings.pinecone_api_key)
    index = pc.Index(settings.pinecone_index)
    out: list[dict] = []
    for i in range(0, len(ids), batch):
        res = index.fetch(ids=ids[i:i + batch], namespace=namespace)
        for vid, vec in (res.get("vectors") or {}).items():
            out.append({"id": vid, "values": vec["values"],
                        "metadata": vec.get("metadata") or {}})
        print(f"  fetched {len(out)}/{len(ids)}", end="\r", flush=True)
    print()
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["copy", "embed"], default="copy")
    ap.add_argument("--namespace", default="products,kb")
    ap.add_argument("--limit", type=int, default=2000)
    a = ap.parse_args()

    await dbmod.init_pool()
    local = get_vector_store()
    backend = type(local).__name__
    print(f"target backend: {backend} | embed_provider={settings.embed_provider} | "
          f"dim={settings.embed_dim}")

    for ns in [n.strip() for n in a.namespace.split(",") if n.strip()]:
        rows = await rows_for(ns, a.limit)
        print(f"\n[{ns}] source rows: {len(rows)}")
        if not rows:
            continue
        if a.mode == "copy":
            vectors = copy_from_pinecone(ns, [r["id"] for r in rows])
            # keep the DB's metadata where Pinecone had none
            by_id = {r["id"]: r for r in rows}
            for v in vectors:
                if not v.get("metadata"):
                    v["metadata"] = by_id.get(v["id"], {}).get("metadata") or {}
        else:
            emb = get_embedder()
            texts = [r["text"] or "" for r in rows]
            vals = await emb.embed_many(texts, concurrency=4)
            vectors = [{"id": r["id"], "values": v, "metadata": r["metadata"] or {}}
                       for r, v in zip(rows, vals)]
            await emb.aclose()
        print(f"  upserting {len(vectors)} vectors into {ns} ...")
        n = await local.upsert(vectors, ns)
        print(f"  upserted {n}")
    print("\nstats:", await local.stats())
    await local.aclose()
    await dbmod.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
