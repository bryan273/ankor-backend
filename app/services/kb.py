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

import html
import json
import os
import re
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


# How many passages the reranker is ever shown, and therefore the hydration window too:
# a passage outside it cannot be reached by any path through `search_kb`.
RERANK_WINDOW = 24


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
        for i, p in enumerate(passages[:RERANK_WINDOW])
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


# Below this, a passage is noise.
#
# Calibrated on gemini-embedding-001, whose cosine range is narrow. Measured here:
# relevant passages score 0.69-0.74, unrelated ones up to 0.68. The bands overlap, so
# this is a floor for obvious junk and nothing more — tightening it to 0.69 to catch the
# last off-topic hit also silenced entire legitimate queries, which is the worse error.
#
# Relevance is therefore defended in three places, not one: this floor removes the
# clearly-unrelated, MAX_PER_ARTICLE stops one document monopolising every slot, and the
# composer is told not to cite a passage it judges irrelevant. Re-measure if the
# embedding model changes — a floor tuned for one model means nothing for another.
MIN_RELEVANCE = float(os.getenv("KB_MIN_RELEVANCE", "0.66"))

# At most this many chunks from any one article.
#
# The symptom that motivated this — "six copies of 'Display related issues for hubs and
# docks' cited as six sources" — was not long articles scoring alike. It was the
# `chunk_text` non-termination bug, which emitted the same paragraph shifted by one
# character hundreds of times; 97% of the index was duplicates. That is fixed, and the
# rebuilt corpus is shallow: 7,597 of 8,071 articles produce ONE chunk, and only 90
# produce more than two, so this cap now binds on roughly 1% of the corpus.
#
# It is kept because it is still the right rule for those 90 — a 12-chunk manual should
# not fill a 6-slot answer — but it is no longer load-bearing, and raising it is a
# defensible change to measure rather than the safety net it used to be.
MAX_PER_ARTICLE = 2


def diversify(chunks: List[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
    """Keep the best chunks while capping how many come from the same article."""
    seen: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    for c in chunks:
        key = str(c.get("article_id") or c.get("title") or c.get("url") or c.get("id"))
        if seen.get(key, 0) >= MAX_PER_ARTICLE:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(c)
        if len(out) >= k:
            break
    return out


async def search_kb(
    query: str, sku: Optional[str] = None, doc_type: Optional[str] = None,
    k: int = 6, use_hyde: bool = True, product_name: str = "",
) -> List[Dict[str, Any]]:
    """The tool the agent calls. Returns citation-ready chunks.

    Returning nothing is a valid and useful answer. The corpus does not cover every
    product, and handing the composer six weakly-related passages invites it to dress
    them up as an answer — far worse than the agent saying it has no guide for this
    device and asking what the customer can see.
    """
    search_text = await hyde_expand(query, product_name) if use_hyde else query
    candidates = await vector_search(search_text, NS_KB, top_k=40, sku=sku, doc_type=doc_type)
    if not candidates and sku:
        # The SKU filter can be too tight when a manual chunk was indexed against a
        # product family rather than a variant. Retry unfiltered before giving up.
        log.info("kb.filter_relaxed", sku=sku)
        candidates = await vector_search(search_text, NS_KB, top_k=25)

    usable = [c for c in candidates if c.get("score", 0) >= MIN_RELEVANCE]
    if not usable:
        log.info("kb.nothing_relevant", query=query[:70],
                 best=round(candidates[0].get("score", 0), 3) if candidates else 0)
        return []

    # Hydrate BEFORE reranking, not after.
    #
    # `vector_search` returns Pinecone metadata, which carries no text — only
    # `article_id, title, url, doc_type, sku, ord`. Reranking that meant handing the
    # model a listing of `[0] Soundcore:` with an empty body for every passage, because
    # `p.get("text")` was always None and 60% of titles are the site name. The rerank
    # prompt is also allowed to DROP passages, so it was discarding real answers on the
    # strength of no information at all. It ran on nearly every query too: it triggers
    # when the top-to-kth score spread is under 0.12, and this embedding model's spread
    # over a good result set is about 0.01.
    #
    # Only the first RERANK_WINDOW are ever shown to the model, and when rerank is
    # skipped it returns `passages[:k]` with k well under that — so hydrating the whole
    # 40-candidate set fetches rows nothing can reach. Measured at 0.74s for 40 rows.
    hydrated = await _hydrate(usable[:RERANK_WINDOW])
    ranked = await rerank(query, hydrated, k=k * 2)
    return diversify(ranked, k)


# 4,826 of the 8,071 indexed articles — 60% — carry one of three site-chrome titles
# instead of their own: `Anker` (1,527), `Soundcore` (1,261), and
# `eufy Support | Troubleshooting &amp; Customer Service` (2,038). The crawler read the
# page's <title>, which on these support pages is the site name.
#
# The bodies are fine; only the label is wrong. But the label is what the composer cites,
# so "how do I reset my earbuds" returns seven correct, distinct reset guides all called
# "Soundcore", and the customer is shown seven identical sources. Deriving the title from
# the body's opening sentence recovers the real one — these articles open by naming
# themselves ("This article will show you how to reset soundcore Liberty 5").
_CHROME_TITLES = {
    "anker", "soundcore", "eufy", "eufy support", "anker solix", "ankermake", "eufymake",
    "eufy support | troubleshooting & customer service",
}
_TITLE_MAX = 90


def _from_slug(url: Optional[str]) -> str:
    """`.../how-to-reset-liberty-4` -> `How to reset liberty 4`. The last resort."""
    slug = (url or "").rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    words = [w for w in re.split(r"[-_]+", slug) if w]
    # Digits are kept — they are usually the model ("liberty-4-nc") — but a slug that is
    # ONLY digits is an opaque article id and makes a worse label than the site name.
    if len([w for w in words if re.search(r"[A-Za-z]", w)]) < 2:
        return ""
    return " ".join(words).capitalize()


def display_title(title: Optional[str], text: Optional[str],
                  url: Optional[str] = None, ord_: int = 0) -> str:
    """The article's own title, or its opening sentence when the title is site chrome.

    `ord_` is which chunk of the article this is, and it decides whether the body may be
    used at all. Only chunk 0 opens where the article opens; a later chunk opens wherever
    the splitter happened to cut, so deriving from it produces labels like "To reset your
    earbuds: 1" or "1. Place the earbuds in the charging case". Those read as titles at a
    glance, which makes them worse than an honest fallback — so later chunks go straight
    to the URL slug.
    """
    clean = html.unescape((title or "").strip())
    if clean and clean.strip().lower().rstrip(" -|") not in _CHROME_TITLES:
        return clean

    body = html.unescape((text or "").strip())
    if body and ord_ == 0:
        # First sentence, or the first line if the opening runs long without punctuation.
        first = body.split("\n", 1)[0].strip()
        match = re.search(r"^(.{10,%d}?[.!?])(?:\s|$)" % _TITLE_MAX, first)
        candidate = (match.group(1) if match else first)[:_TITLE_MAX].strip(" .,-:")
        # Even chunk 0 can start with boilerplate ("Method 1: 1"). Require three real
        # words — counting tokens is not enough, since that string is three of them.
        if len(re.findall(r"[A-Za-z]{3,}", candidate)) >= 3:
            return candidate

    return _from_slug(url) or clean


async def _hydrate(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Vector metadata carries a pointer, not the whole chunk. Pull the text and the
    article title from Postgres so citations have somewhere real to point."""
    ids = [c["id"] for c in chunks if c.get("id")]
    if not ids:
        return chunks
    rows = await db.fetch(
        """
        select kc.pinecone_id, kc.text, kc.ord, kc.meta, ka.title, ka.source_url, ka.doc_type
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
            out.append({**c, "text": row["text"],
                        "title": display_title(row["title"], row["text"], row["source_url"],
                                               row["ord"]),
                        "source_title": row["title"],
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
