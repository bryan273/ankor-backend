"""Knowledge retrieval — the RAG half of the agent.

Two ideas do most of the work here.

**HyDE.** Users write "it won't suck anymore"; manuals write "reduced suction
performance". Embedding the user's words and hoping they land near the manual's words
is the single biggest recall loss in support RAG. So we ask the model to write the
paragraph that *would* answer the question, and embed that instead. The hypothetical
document lives in manual-space, which is where the real answer lives too.

**SKU filtering.** Once a product is resolved, every vector query is filtered to it. A
robot-vacuum instruction surfacing in a breast-pump thread is the worst failure this
system can make, and a metadata filter costs nothing.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import structlog

from app.clients import db
from app.clients.embed import get_embedder
from app.clients.llm import get_llm
from app.clients.vectors import NS_COMMUNITY, NS_KB, NS_PRODUCTS, NS_TICKETS, get_vectors

log = structlog.get_logger(__name__)

HYDE_PROMPT = """You write the paragraph a product manual would contain to answer this \
support question. Write it the way a manual writes: neutral, specific, using the \
technical vocabulary the manufacturer would use rather than the customer's words.

Two or three sentences. No preamble, no "here is", just the paragraph.

Question: {question}
Product: {product}"""

RERANK_PROMPT = """Rank these documentation passages by how directly they answer the \
support question. A passage that names the exact symptom and gives an action beats one \
that merely mentions the product.

Question: {question}

Passages:
{passages}

Reply with JSON only: {{"ranked": [<passage numbers, best first>]}} — at most {k} numbers, \
and drop any passage that does not help."""


async def hyde_expand(question: str, product: str = "") -> str:
    """Rewrite a symptom into manual language. Falls back to the raw question — a
    failed expansion should degrade recall, never the turn."""
    try:
        text, usage = await get_llm().complete(
            [{"role": "user", "content": HYDE_PROMPT.format(
                question=question, product=product or "unknown")}],
            max_tokens=1200,
        )
        expanded = text.strip()
        return f"{question}\n\n{expanded}" if expanded else question
    except Exception as e:  # noqa: BLE001
        log.warning("kb.hyde_failed", error=str(e)[:140])
        return question


async def vector_search(
    query: str, namespace: str = NS_KB, top_k: int = 30,
    sku: Optional[str] = None, doc_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    embedder = get_embedder()
    vector = await embedder.embed_query(query)
    flt: Dict[str, Any] = {}
    if sku:
        flt["sku"] = {"$in": [sku]}
    if doc_type:
        flt["doc_type"] = {"$eq": doc_type}
    matches = await get_vectors().query(vector, namespace=namespace, top_k=top_k,
                                        flt=flt or None)
    return [
        {"id": m["id"], "score": m.get("score", 0.0), **(m.get("metadata") or {})}
        for m in matches
    ]


async def rerank(question: str, passages: List[Dict[str, Any]], k: int = 6) -> List[Dict[str, Any]]:
    """Listwise LLM rerank, but only when the scores are close enough to be worth it.

    If the top result is clearly ahead of the pack, an extra model call buys nothing
    and costs ~3,800 input tokens on every turn.
    """
    if len(passages) <= k:
        return passages
    top = passages[0].get("score", 0)
    kth = passages[min(k, len(passages)) - 1].get("score", 0)
    if top - kth > 0.12:
        return passages[:k]

    listing = "\n\n".join(
        f"[{i}] {(p.get('title') or p.get('section') or 'passage')}: "
        f"{(p.get('text') or '')[:400]}"
        for i, p in enumerate(passages[:24])
    )
    try:
        data, usage = await get_llm().json_complete(
            [{"role": "user", "content": RERANK_PROMPT.format(
                question=question, passages=listing, k=k)}],
            max_tokens=1200, default={"ranked": []},
        )
        order = [i for i in data.get("ranked", []) if isinstance(i, int) and 0 <= i < len(passages)]
        if order:
            return [passages[i] for i in order[:k]]
    except Exception as e:  # noqa: BLE001
        log.warning("kb.rerank_failed", error=str(e)[:140])
    return passages[:k]


async def search_kb(
    query: str, sku: Optional[str] = None, doc_type: Optional[str] = None,
    k: int = 6, use_hyde: bool = True, product_name: str = "",
) -> List[Dict[str, Any]]:
    """The tool the agent calls. Returns citation-ready chunks."""
    search_text = await hyde_expand(query, product_name) if use_hyde else query
    candidates = await vector_search(search_text, NS_KB, top_k=30, sku=sku, doc_type=doc_type)
    if not candidates and sku:
        # The SKU filter can be too tight when a manual chunk was indexed against a
        # product family rather than a variant. Retry unfiltered before giving up.
        log.info("kb.filter_relaxed", sku=sku)
        candidates = await vector_search(search_text, NS_KB, top_k=20)
    ranked = await rerank(query, candidates, k=k)
    return await _hydrate(ranked)


async def _hydrate(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Vector metadata carries a pointer, not the whole chunk. Pull the text and the
    article title from Postgres so citations have somewhere real to point."""
    ids = [c["id"] for c in chunks if c.get("id")]
    if not ids:
        return chunks
    rows = await db.fetch(
        """
        select kc.pinecone_id, kc.text, kc.meta, ka.title, ka.source_url, ka.doc_type
        from kb_chunks kc join kb_articles ka on ka.id = kc.article_id
        where kc.pinecone_id = any(%s)
        """,
        (ids,),
    )
    by_id = {r["pinecone_id"]: r for r in rows}
    out = []
    for c in chunks:
        row = by_id.get(c.get("id"))
        if row:
            meta = row.get("meta") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            out.append({**c, "text": row["text"], "title": row["title"],
                        "url": row["source_url"], "doc_type": row["doc_type"],
                        "section": meta.get("section", ""), "page": meta.get("page")})
        else:
            out.append(c)
    return out


async def search_products_vector(query: str, k: int = 8,
                                 category: Optional[str] = None) -> List[Dict[str, Any]]:
    flt = {"category": {"$eq": category}} if category else None
    embedder = get_embedder()
    vector = await embedder.embed_query(query)
    matches = await get_vectors().query(vector, namespace=NS_PRODUCTS, top_k=k, flt=flt)
    return [{"score": m.get("score", 0), **(m.get("metadata") or {})} for m in matches]


async def search_tickets(query: str, k: int = 5,
                         sku: Optional[str] = None) -> List[Dict[str, Any]]:
    """Historical resolutions. Often the fastest route to a real fix, because someone
    already had this exact problem.

    Deduplicated by resolution text. A common fault genuinely produces many tickets with
    the same outcome, and near-identical text embeds to near-identical vectors — so an
    undeduplicated top-5 can be five copies of one answer, which is both useless to the
    agent and four wasted slots that a different fix could have used.
    """
    matches = await vector_search(query, NS_TICKETS, top_k=k * 4, sku=sku)
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for m in matches:
        fingerprint = (str(m.get("resolution", ""))[:120].strip().lower(),
                       str(m.get("symptom", ""))[:80].strip().lower())
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(m)
        if len(unique) >= k:
            break
    return unique


async def search_community(query: str, k: int = 5) -> List[Dict[str, Any]]:
    return await vector_search(query, NS_COMMUNITY, top_k=k)


async def get_troubleshooting_flow(symptom: str,
                                   product_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Curated step lists beat generated ones: they were written by someone who has
    seen the device. Trigram match so "won't suck" finds "reduced suction"."""
    if product_id:
        row = await db.fetch_one(
            """
            select id::text as flow_id, symptom, steps, est_minutes, source_url
            from troubleshooting_flows
            where product_id = %s
            order by similarity(symptom, %s) desc limit 1
            """,
            (product_id, symptom),
        )
        if row:
            return row
    return await db.fetch_one(
        """
        select tf.id::text as flow_id, tf.symptom, tf.steps, tf.est_minutes, tf.source_url,
               p.sku, p.name
        from troubleshooting_flows tf left join products p on p.id = tf.product_id
        where similarity(tf.symptom, %s) > 0.25
        order by similarity(tf.symptom, %s) desc limit 1
        """,
        (symptom, symptom),
    )
