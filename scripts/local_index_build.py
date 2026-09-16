#!/usr/bin/env python3
"""Build the local retrieval stack end to end: no cloud, no quota.

  1. pull kb_articles text from the shared project into the LOCAL Postgres
     (so the app can run with DATABASE_URL pointing at .localdb)
  2. chunk it with the team's own chunker (`scripts/embed_corpus.py::chunk_text`,
     imported from the file so the local chunking matches the cloud exactly)
  3. embed the chunks on this machine (EMBED_PROVIDER=fastembed -> ONNX, no torch,
     no server, no Google quota)
  4. write the vectors into the local vector store AND the kb_chunks rows into local
     Postgres - both are needed: app/services/kb.py resolves citations with
     `kb_chunks join kb_articles`, so vectors alone would return text with no source

Usage
    python scripts/local_index_build.py --limit-articles 400 --resume
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import pathlib
import sys
import time
import urllib.request
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.adapters import get_embedder, get_vector_store  # noqa: E402
from app.clients import db as dbmod  # noqa: E402
from app.config import settings  # noqa: E402

CORE = pathlib.Path(__file__).resolve().parent.parent
SUPA = "https://wmxucgywnafvlnybkjhi.supabase.co/rest/v1"


def load_chunk_text():
    """Import chunk_text() from scripts/embed_corpus.py without importing the module
    side effects (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("embed_corpus",
                                                  CORE / "scripts" / "embed_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    src = (CORE / "scripts" / "embed_corpus.py").read_text()
    ns: dict[str, Any] = {}
    # execute only the function definition we need
    start = src.index("def chunk_text(")
    nxt = src.find("\ndef ", start + 10)
    end = nxt if nxt != -1 else len(src)
    exec(compile(src[start:end], "chunk_text", "exec"), ns)  # noqa: S102
    return ns["chunk_text"]


def rest_pages(select: str, page: int = 500) -> list[dict]:
    key = (CORE.parent / ".secrets" / "supabase_secret_key").read_text().strip()
    out, off = [], 0
    while True:
        req = urllib.request.Request(
            f"{SUPA}/kb_articles?select={select}&limit={page}&offset={off}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"})
        rows = json.loads(urllib.request.urlopen(req, timeout=120).read())
        out += rows
        print(f"  fetched {len(out)} articles from the cloud", end="\r", flush=True)
        if len(rows) < page:
            break
        off += page
    print()
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-articles", type=int, default=0)
    ap.add_argument("--resume", action="store_true", help="skip articles already chunked")
    ap.add_argument("--batch", type=int, default=64, help="chunks per embed call")
    a = ap.parse_args()

    chunk_text = load_chunk_text()
    await dbmod.init_pool()

    # ---- 1. local kb_articles ------------------------------------------------
    have = await dbmod.fetch("select count(*) as n from kb_articles")
    print(f"local kb_articles before: {have[0]['n']}")
    if True:                      # always top up; on conflict (source_url) is a no-op
        arts = rest_pages("id,source_url,title,doc_type,body,lang")
        print(f"inserting {len(arts)} articles into the local Postgres (idempotent) ...")
        for i in range(0, len(arts), 200):
            rows = [(x["id"], x["source_url"], x.get("title"), x.get("doc_type"),
                     [], x.get("body"), x.get("lang") or "en")   # no UUID mapping in the scrape
                    for x in arts[i:i + 200]]
            await dbmod.execute_many(
                "insert into kb_articles (id, source_url, title, doc_type, product_ids, body, lang)"
                " values (%s,%s,%s,%s,%s,%s,%s) on conflict (source_url) do nothing", rows)

    sql = ("select id::text as id, source_url, title, body from kb_articles "
           "where body is not null and length(body) > 100")
    if a.resume:
        sql += (" and not exists (select 1 from kb_chunks c where c.article_id = kb_articles.id)")
    sql += " order by id"
    if a.limit_articles:
        sql += f" limit {a.limit_articles}"
    arts = await dbmod.fetch(sql)
    print(f"articles to index: {len(arts)}")

    # ---- 2. chunk ------------------------------------------------------------
    plan: list[dict] = []
    for art in arts:
        for ordn, text in enumerate(chunk_text(art["body"] or "")):
            plan.append({"article_id": art["id"], "ord": ordn, "text": text,
                         "hash": hashlib.sha256(text.encode()).hexdigest(),
                         "url": art["source_url"], "title": art["title"] or ""})
    print(f"chunks produced: {len(plan)} (avg {len(plan)/max(len(arts),1):.1f}/article)")

    # ---- 3+4. embed locally, write vectors + rows ----------------------------
    emb = get_embedder()
    store = get_vector_store()
    print(f"embedder={type(emb).__name__} | store={type(store).__name__}")
    t0 = time.time()
    written = 0
    for i in range(0, len(plan), a.batch):
        batch = plan[i:i + a.batch]
        vecs = await emb.embed_many([b["text"] for b in batch], concurrency=4)
        if i == 0:
            print(f"  embedding dim = {len(vecs[0])}")
        vectors = [{"id": f"{b['article_id']}:{b['ord']}", "values": v,
                    "metadata": {"section": b["title"], "url": b["url"],
                                 "article_id": b["article_id"], "ord": b["ord"]}}
                   for b, v in zip(batch, vecs)]
        await store.upsert(vectors, "kb")
        await dbmod.execute_many(
            "insert into kb_chunks (article_id, ord, text, text_hash, pinecone_id, meta, embedded_at)"
            " values (%s,%s,%s,%s,%s,%s, now())"
            " on conflict (article_id, ord) do update set text = excluded.text,"
            " text_hash = excluded.text_hash, pinecone_id = excluded.pinecone_id,"
            " meta = excluded.meta, embedded_at = now()",
            [(b["article_id"], b["ord"], b["text"], b["hash"],
              f"{b['article_id']}:{b['ord']}",
              json.dumps({"section": b["title"], "url": b["url"]})) for b in batch])
        written += len(batch)
        if written % (a.batch * 5) < a.batch:
            rate = written / max(time.time() - t0, 1e-6)
            print(f"  {written}/{len(plan)} chunks  ({rate:.0f}/s)", flush=True)

    print(f"indexed {written} chunks in {time.time()-t0:.0f}s")
    print("store stats:", await store.stats())
    local_chunks = await dbmod.fetch("select count(*) as n from kb_chunks")
    print("local kb_chunks rows:", local_chunks[0]["n"])
    await emb.aclose()
    await store.aclose()
    await dbmod.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
