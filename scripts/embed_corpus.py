"""Embed the corpus into Pinecone.

Content-hash driven: every chunk carries a `text_hash`, and a chunk whose hash has not
changed is skipped rather than re-embedded. A second run over an unchanged corpus costs
nothing, which is what makes iterating on the crawler affordable.

Namespaces keep the corpora apart (`products`, `kb`, `tickets`, `dealers`). That
separation is not tidiness: it is what stops a robot-vacuum manual chunk surfacing in a
breast-pump conversation, which is the worst failure this system can make.

    python scripts/embed_corpus.py                 # everything, incremental
    python scripts/embed_corpus.py --only products
    python scripts/embed_corpus.py --force         # re-embed regardless of hash
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import uuid
from typing import Any, Dict, List, Optional

import structlog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402
from app.clients.embed import EMBED_PRICE_PER_TOKEN, get_embedder, text_hash  # noqa: E402
from app.clients.vectors import (NS_DEALERS, NS_KB, NS_PRODUCTS,  # noqa: E402
                                 NS_TICKETS, get_vectors)

log = structlog.get_logger("embed")

# Matches `embed.BATCH_LIMIT`: batchEmbedContents takes 100 contents per request,
# and the whole point of the change is to send as few requests as the API allows.
BATCH = 100


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


async def embed_batch(texts: List[str]) -> List[List[float]]:
    return await get_embedder().embed_many(texts, "RETRIEVAL_DOCUMENT", concurrency=8)


# ── products ──────────────────────────────────────────────────────────────────

def product_text(row: Dict[str, Any]) -> str:
    """One vector per product: what it is, what it costs, what it looks like.

    The image caption is folded into the same text rather than embedded separately —
    text-space multimodality. It costs nothing extra, works with any text embedder, and
    the image URL rides along in metadata so a retrieved chunk can still show a picture.
    """
    raw = row.get("raw") or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    parts = [
        f"{row['brand']} {row['name']}",
        f"Category: {row.get('category') or 'unknown'}",
        # `.get(key, "")` returns None when the key EXISTS with a null value, and the
        # slice then raises. That is what stopped this script at ~898 of 1,493 products:
        # it crashed mid-run, and because nothing checked the exit code the catalogue sat
        # 40% unindexed while every other namespace looked healthy.
        (raw.get("description") or "")[:600],
    ]
    if row.get("specs"):
        parts.append("Specifications: " + "; ".join(
            f"{k}: {v}" for k, v in list(row["specs"].items())[:12]))
    if row.get("captions"):
        parts.append("Appearance: " + " ".join(row["captions"][:3]))
    if row.get("aliases"):
        parts.append("Also called: " + ", ".join(row["aliases"][:8]))
    return "\n".join(p for p in parts if p).strip()


async def embed_products(force: bool = False) -> int:
    rows = await db.fetch(
        """
        select p.id::text as id, p.sku, p.name, p.brand, p.category, p.price, p.url,
               p.hero_image, p.status, p.raw,
               coalesce(
                 (select jsonb_object_agg(s.key, s.value)
                  from product_specs s where s.product_id = p.id), '{}'::jsonb) as specs,
               coalesce(
                 (select array_agg(m.caption) from product_media m
                  where m.product_id = p.id and m.caption is not null), '{}') as captions,
               coalesce(
                 (select array_agg(a.alias) from product_aliases a
                  where a.product_id = p.id), '{}') as aliases
        from products p
        where p.status <> 'invalid'
        """
    )
    if not rows:
        return 0

    vectors: List[Dict[str, Any]] = []
    texts, pending = [], []
    for row in rows:
        text = product_text(row)
        if len(text) < 20:
            continue
        texts.append(text)
        pending.append(row)

    total = 0
    for i in range(0, len(texts), BATCH):
        chunk_texts = texts[i:i + BATCH]
        chunk_rows = pending[i:i + BATCH]
        values = await embed_batch(chunk_texts)
        for row, vector, text in zip(chunk_rows, values, chunk_texts):
            vectors.append({
                "id": f"prod_{row['id']}",
                "values": vector,
                "metadata": {
                    "sku": row["sku"], "name": row["name"], "brand": row["brand"],
                    "category": row.get("category") or "", "url": row.get("url") or "",
                    "image_url": row.get("hero_image") or "",
                    "price": float(row["price"]) if row.get("price") else 0.0,
                    "status": row.get("status") or "active",
                    "text": text[:1500],
                },
            })
        if len(vectors) >= 100:
            total += await get_vectors().upsert(vectors, NS_PRODUCTS)
            vectors = []
        log.info("embed.products_progress", done=min(i + BATCH, len(texts)), of=len(texts))
    if vectors:
        total += await get_vectors().upsert(vectors, NS_PRODUCTS)
    log.info("embed.products_done", n=total)
    return total


# ── knowledge base ────────────────────────────────────────────────────────────

def chunk_text(text: str, size: int = 3200, overlap: int = 480) -> List[str]:
    """Split on paragraph boundaries where possible; a chunk that ends mid-sentence
    retrieves badly because the embedding describes half a thought."""
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            for boundary in ("\n\n", "\n", ". "):
                cut = text.rfind(boundary, start + size // 2, end)
                if cut > 0:
                    end = cut + len(boundary)
                    break
        chunks.append(text[start:end].strip())
        # Stop at the end of the text. Without this the loop degenerates: once `end` is
        # len(text), `end - overlap` is behind `start`, so `start` advances ONE CHARACTER
        # per iteration and emits a near-identical chunk each time. A 3,494-character
        # article produced 402 chunks that way — the index filled with copies of the same
        # paragraph offset by a character, which is also why `kb.MAX_PER_ARTICLE` had to
        # exist to stop one article swamping every top-k.
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if len(c) > 80]


async def embed_kb(force: bool = False) -> int:
    """Embed the articles worth embedding, using their cleaned text.

    Two filters, both added after a bulk import grew the table from 2.7k to 14.8k rows:

    `embed_status <> 'chrome'/'non_english'` — `scripts/triage_kb_corpus.py` marks rows
    that are navigation rails, question-title lists or not in English. Embedding those
    spends a request each to teach the agent about sale banners, and the corpus already
    demonstrated the cost of it: a wrong-product article that clears the relevance floor
    answers confidently and wrongly.

    `coalesce(clean_body, body)` — the cleaned text with the rails stripped. Rows that
    predate the triage have no `clean_body` and fall back to their original body, so
    this is safe to run before the triage as well as after.
    """
    articles = await db.fetch(
        "select id::text as id, source_url, title, doc_type, "
        "       coalesce(clean_body, body) as body, product_ids "
        "from kb_articles "
        "where coalesce(clean_body, body) is not null "
        "  and length(coalesce(clean_body, body)) > 100 "
        "  and coalesce(embed_status, 'keep') = 'keep'")
    if not articles:
        log.info("embed.kb_empty")
        return 0

    # Two lookups for the whole run instead of two per chunk.
    #
    # This loop used to issue one `select … from kb_chunks where article_id = … and
    # ord = …` for EVERY chunk, plus one SKU query per article. Against the Supabase
    # pooler that is ~7,500 sequential round-trips before the first vector is computed —
    # the job appeared hung for forty minutes while it did nothing but ask the database
    # questions it could have asked once. The embedding API was never the bottleneck.
    log.info("embed.kb_preload", articles=len(articles))
    existing_chunks: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for row in await db.fetch(
        "select article_id::text as article_id, ord, text_hash, pinecone_id "
        "from kb_chunks"
    ):
        existing_chunks.setdefault(row["article_id"], {})[row["ord"]] = row

    sku_by_product: Dict[str, str] = {
        r["id"]: r["sku"]
        for r in await db.fetch("select id::text as id, sku from products")
    }

    # One buffer for the WHOLE run, not one per article.
    #
    # The flush used to live inside the article loop — `for i in range(0, len(to_embed),
    # BATCH)` over a single article's chunks. Articles average under two chunks each, so a
    # 100-content window never filled: the run issued one embed request, one Pinecone
    # upsert and one DB round-trip PER ARTICLE. Measured at 36 articles/min, which is
    # 3.7 hours and ~8,000 requests for this corpus — the request-per-chunk pattern that
    # already cost this project a Google account, wearing a different hat. Chunks now
    # queue across articles and go out only when a full batch exists.
    pending: List[Dict[str, Any]] = []
    total, skipped, done = 0, 0, 0

    async def flush() -> int:
        """Embed, upsert and record everything queued. Leaves `pending` empty."""
        if not pending:
            return 0
        values = await embed_batch([p["text"] for p in pending])
        vectors = [
            {"id": p["pinecone_id"], "values": vector,
             "metadata": {"article_id": p["article_id"], "title": p["title"],
                          "url": p["url"], "doc_type": p["doc_type"],
                          "sku": p["skus"], "ord": p["ord"]}}
            for p, vector in zip(pending, values)
        ]
        written = await get_vectors().upsert(vectors, NS_KB)
        await db.execute_many(
            """
            insert into kb_chunks (article_id, ord, text, text_hash, pinecone_id, meta,
                                   embedded_at)
            values (%s,%s,%s,%s,%s,%s, now())
            on conflict (article_id, ord) do update set
                text = excluded.text, text_hash = excluded.text_hash,
                pinecone_id = excluded.pinecone_id, embedded_at = now()
            """,
            [(p["article_id"], p["ord"], p["text"], p["hash"], p["pinecone_id"],
              json.dumps({"section": p["title"], "url": p["url"]}))
             for p in pending],
        )
        pending.clear()
        return written

    for article in articles:
        pieces = chunk_text(article["body"])
        skus: List[str] = [
            sku_by_product[str(pid)] for pid in (article.get("product_ids") or [])
            if str(pid) in sku_by_product
        ]
        seen_chunks = existing_chunks.get(article["id"], {})

        for ord_, piece in enumerate(pieces):
            h = text_hash(piece)
            existing = seen_chunks.get(ord_)
            if existing and existing["text_hash"] == h and existing["pinecone_id"] and not force:
                skipped += 1
                continue
            pending.append({
                "article_id": article["id"], "ord": ord_, "text": piece, "hash": h,
                "pinecone_id": ((existing or {}).get("pinecone_id")
                                or f"kb_{uuid.uuid4().hex[:16]}"),
                "title": article["title"] or "", "url": article["source_url"],
                "doc_type": article["doc_type"] or "", "skus": skus,
            })
            # Appends are one at a time, so this trips at exactly BATCH.
            if len(pending) >= BATCH:
                total += await flush()

        done += 1
        if done % 500 == 0:
            log.info("embed.kb_progress", articles=done, of=len(articles),
                     embedded=total, queued=len(pending), skipped=skipped)

    total += await flush()
    log.info("embed.kb_done", embedded=total, skipped_unchanged=skipped)
    return total


# ── historical tickets ────────────────────────────────────────────────────────

async def embed_tickets(force: bool = False) -> int:
    rows = await db.fetch(
        """
        select t.id::text as id, t.summary, p.sku, p.category,
               (select te.payload->>'note' from ticket_events te
                where te.ticket_id = t.id and te.kind = 'resolved' limit 1) as resolution
        from tickets t left join products p on p.id = t.product_id
        where t.status = 'resolved' and t.summary is not null
        """
    )
    rows = [r for r in rows if r.get("resolution")]
    if not rows:
        return 0
    total = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        texts = [f"Problem: {r['summary']}\nResolution: {r['resolution']}" for r in batch]
        values = await embed_batch(texts)
        vectors = [{
            "id": f"tkt_{r['id']}", "values": v,
            "metadata": {"symptom": r["summary"], "resolution": r["resolution"],
                         "sku": r.get("sku") or "", "category": r.get("category") or ""},
        } for r, v in zip(batch, values)]
        total += await get_vectors().upsert(vectors, NS_TICKETS)
    log.info("embed.tickets_done", n=total)
    return total


# ── dealers ───────────────────────────────────────────────────────────────────

async def embed_dealers(force: bool = False) -> int:
    rows = await db.fetch(
        "select id::text as id, name, region, order_no_pattern, service_path, authorized "
        "from dealers")
    if not rows:
        return 0
    texts = [
        f"{r['name']} — authorised dealer in {r['region']}. "
        f"Invoice numbers look like {r['order_no_pattern']}. "
        f"Service route: {r['service_path']}"
        if r["authorized"] else
        f"{r['name']} in {r['region']} is NOT an authorised dealer. "
        f"Invoice numbers look like {r['order_no_pattern']}. {r['service_path']}"
        for r in rows
    ]
    values = await embed_batch(texts)
    vectors = [{
        "id": f"dlr_{r['id']}", "values": v,
        "metadata": {"dealer_id": r["id"], "name": r["name"], "region": r["region"],
                     "authorized": bool(r["authorized"]),
                     "service_path": r["service_path"] or "", "text": t},
    } for r, v, t in zip(rows, values, texts)]
    n = await get_vectors().upsert(vectors, NS_DEALERS)
    log.info("embed.dealers_done", n=n)
    return n


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="products|kb|tickets|dealers")
    parser.add_argument("--force", action="store_true", help="ignore content hashes")
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(processors=[structlog.processors.add_log_level,
                                    structlog.dev.ConsoleRenderer(colors=False)])

    await db.init_pool()
    counts: Dict[str, int] = {}
    try:
        wanted = {w.strip() for w in args.only.split(",") if w.strip()} or \
            {"products", "kb", "tickets", "dealers"}
        if "products" in wanted:
            counts["products"] = await embed_products(args.force)
        if "kb" in wanted:
            counts["kb"] = await embed_kb(args.force)
        if "tickets" in wanted:
            counts["tickets"] = await embed_tickets(args.force)
        if "dealers" in wanted:
            counts["dealers"] = await embed_dealers(args.force)

        stats = await get_vectors().stats()
        print("\nPinecone index now holds:")
        for ns, info in (stats.get("namespaces") or {}).items():
            print(f"  {ns:12} {info.get('vectorCount', 0)} vectors")
        print(f"  {'TOTAL':12} {stats.get('totalVectorCount', 0)}")
    finally:
        await get_vectors().aclose()
        await get_embedder().aclose()
        await db.close_pool()

    print("\nembedded this run: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
