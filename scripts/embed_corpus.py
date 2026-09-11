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

BATCH = 32


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
        raw.get("description", "")[:600],
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
        start = max(end - overlap, start + 1)
    return [c for c in chunks if len(c) > 80]


async def embed_kb(force: bool = False) -> int:
    articles = await db.fetch(
        "select id::text as id, source_url, title, doc_type, body, product_ids "
        "from kb_articles where body is not null and length(body) > 100")
    if not articles:
        log.info("embed.kb_empty")
        return 0

    total, skipped = 0, 0
    for article in articles:
        pieces = chunk_text(article["body"])
        skus: List[str] = []
        if article.get("product_ids"):
            rows = await db.fetch(
                "select sku from products where id::text = any(%s)",
                ([str(p) for p in article["product_ids"]],))
            skus = [r["sku"] for r in rows]

        to_embed, meta_rows = [], []
        for ord_, piece in enumerate(pieces):
            h = text_hash(piece)
            existing = await db.fetch_one(
                "select pinecone_id, text_hash from kb_chunks where article_id = %s and ord = %s",
                (article["id"], ord_))
            if existing and existing["text_hash"] == h and existing["pinecone_id"] and not force:
                skipped += 1
                continue
            pinecone_id = (existing or {}).get("pinecone_id") or f"kb_{uuid.uuid4().hex[:16]}"
            to_embed.append(piece)
            meta_rows.append({"ord": ord_, "hash": h, "pinecone_id": pinecone_id,
                              "text": piece})

        for i in range(0, len(to_embed), BATCH):
            batch_texts = to_embed[i:i + BATCH]
            batch_meta = meta_rows[i:i + BATCH]
            values = await embed_batch(batch_texts)
            vectors = []
            for meta, vector in zip(batch_meta, values):
                vectors.append({
                    "id": meta["pinecone_id"], "values": vector,
                    "metadata": {
                        "article_id": article["id"], "title": article["title"] or "",
                        "url": article["source_url"], "doc_type": article["doc_type"] or "",
                        "sku": skus, "ord": meta["ord"],
                    },
                })
            total += await get_vectors().upsert(vectors, NS_KB)
            await db.execute_many(
                """
                insert into kb_chunks (article_id, ord, text, text_hash, pinecone_id, meta,
                                       embedded_at)
                values (%s,%s,%s,%s,%s,%s, now())
                on conflict (article_id, ord) do update set
                    text = excluded.text, text_hash = excluded.text_hash,
                    pinecone_id = excluded.pinecone_id, embedded_at = now()
                """,
                [(article["id"], m["ord"], m["text"], m["hash"], m["pinecone_id"],
                  json.dumps({"section": article["title"] or "", "url": article["source_url"]}))
                 for m in batch_meta],
            )
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
