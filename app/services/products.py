"""Catalog lookups and product disambiguation.

Disambiguation (SPECIFICATION §3.7, scenario S2) starts deterministically. "S1 Pro"
maps to two products across two brands — a robot vacuum and a wearable breast pump —
and the alias table knows that without a model call. Vector search is the fallback for
phrasing the table has never seen, not the first move.

The discriminator order is deliberate: what the customer owns beats what they said,
what they said beats what a photo suggests, and asking is the last resort. Each step
that resolves silently must say so in the answer, so a wrong guess is cheap to correct.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import structlog

from app.clients import db

log = structlog.get_logger(__name__)

# Symptom vocabulary that discriminates between form factors sharing a model name.
# Only unambiguous terms belong here: "suction" fits both a vacuum and a pump, so it
# is absent on purpose.
CATEGORY_SIGNALS: Dict[str, List[str]] = {
    "robot_vacuum": ["mop", "mopping", "dustbin", "dust bin", "brush roll", "carpet", "floor",
                     "docking station", "self-empty", "lidar", "vacuum", "sweeping", "roller",
                     "side brush", "hepa", "cleans the floor"],
    "breast_pump": ["milk", "let-down", "letdown", "nipple", "flange", "breast", "pumping session",
                    "expressing", "nursing", "baby", "bottle", "areola", "suction cup",
                    "wearable pump"],
    "charger": ["usb-c", "usb c", "wall plug", "gan", "watt", "charging brick", "power delivery",
                "socket", "outlet", "charge my phone"],
    "power_bank": ["power bank", "portable charger", "mah", "recharge on the go"],
    "power_station": ["solar", "camping", "generator", "inverter", "blackout", "kwh"],
    "audio": ["earbuds", "headphones", "bass", "anc", "noise cancelling", "pairing", "bluetooth",
              "left earbud", "right earbud", "soundstage"],
    "security_camera": ["motion detection", "night vision", "footage", "doorbell", "homebase",
                        "recording", "camera feed"],
    "projector": ["lumens", "projection", "screen size", "keystone", "focus"],
}


def normalise_alias(text: str) -> str:
    """Aliases are matched on a squashed lowercase form so 'S1-Pro', 'S1 pro' and
    's1pro' all land on the same key."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


async def find_by_alias(mention: str) -> List[Dict[str, Any]]:
    """Products whose alias table contains this mention. The heart of S2."""
    alias = normalise_alias(mention)
    if not alias:
        return []
    rows = await db.fetch(
        """
        select p.id::text as product_id, p.sku, p.name, p.brand, p.category,
               p.hero_image, p.price, p.status, a.confidence
        from product_aliases a
        join products p on p.id = a.product_id
        where a.alias = %s
        order by a.confidence desc, p.name
        """,
        (alias,),
    )
    if rows:
        return rows
    # Substring fallback: "my S1 Pro robot" contains the alias but is not equal to it.
    return await db.fetch(
        """
        select distinct p.id::text as product_id, p.sku, p.name, p.brand, p.category,
               p.hero_image, p.price, p.status, a.confidence
        from product_aliases a
        join products p on p.id = a.product_id
        where %s like '%%' || a.alias || '%%' and length(a.alias) >= 4
        order by a.confidence desc, p.name
        limit 12
        """,
        (alias,),
    )


async def search_products(
    query: str = "", brand: Optional[str] = None, category: Optional[str] = None,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """Trigram search over names — the catalog page and the `search_products` tool."""
    clauses, params = ["1=1"], []
    if query:
        clauses.append("(p.name ilike %s or p.sku ilike %s or similarity(p.name, %s) > 0.2)")
        params += [f"%{query}%", f"%{query}%", query]
    if brand:
        clauses.append("p.brand ilike %s")
        params.append(brand)
    if category:
        clauses.append("p.category = %s")
        params.append(category)
    order = "similarity(p.name, %s) desc, p.name" if query else "p.name"
    if query:
        params.append(query)
    params.append(limit)
    return await db.fetch(
        f"""
        select p.id::text as product_id, p.sku, p.name, p.brand, p.category, p.price,
               p.currency, p.url, p.hero_image, p.status, p.warranty_months
        from products p
        where {' and '.join(clauses)}
        order by {order}
        limit %s
        """,
        params,
    )


async def get_product(sku: str) -> Optional[Dict[str, Any]]:
    row = await db.fetch_one(
        """
        select p.id::text as product_id, p.sku, p.name, p.brand, p.category, p.price,
               p.currency, p.url, p.hero_image, p.status, p.warranty_months, p.raw
        from products p where p.sku = %s
        """,
        (sku,),
    )
    if not row:
        return None
    pid = row["product_id"]
    specs = await db.fetch(
        "select key, value, unit from product_specs where product_id = %s order by key", (pid,))
    media = await db.fetch(
        "select url, kind, caption, ord from product_media where product_id = %s order by ord",
        (pid,))
    docs = await db.fetch(
        "select kind, title, url, pages from product_docs where product_id = %s", (pid,))
    codes = await db.fetch(
        "select code, meaning, severity, fix_steps from error_codes where product_id = %s "
        "order by code", (pid,))
    aliases = await db.fetch(
        "select alias from product_aliases where product_id = %s order by alias", (pid,))
    row["specs"] = {s["key"]: f"{s['value']}{' ' + s['unit'] if s.get('unit') else ''}"
                    for s in specs}
    row["media"] = media
    row["docs"] = docs
    row["error_codes"] = codes
    row["aliases"] = [a["alias"] for a in aliases]
    return row


async def owned_products(customer_id: Optional[str], candidate_ids: List[str]) -> List[str]:
    """Which of these candidates has this customer actually bought? The strongest
    discriminator we have, and it costs one query."""
    if not customer_id or not candidate_ids:
        return []
    rows = await db.fetch(
        """
        select distinct oi.product_id::text as product_id
        from order_items oi
        join orders o on o.id = oi.order_id
        where o.customer_id = %s and oi.product_id::text = any(%s)
        """,
        (customer_id, candidate_ids),
    )
    return [r["product_id"] for r in rows]


def score_by_symptom(candidates: List[Dict[str, Any]], text: str) -> Optional[Dict[str, Any]]:
    """Pick a candidate whose category the user's own words point at.

    Returns a winner only when exactly one category matches. Two matches means the
    text genuinely does not discriminate, and guessing there is how a breast-pump
    question gets answered with vacuum instructions.
    """
    if not text:
        return None
    lowered = text.lower()
    hits: Dict[str, int] = {}
    for category, signals in CATEGORY_SIGNALS.items():
        n = sum(1 for s in signals if s in lowered)
        if n:
            hits[category] = n
    if len(hits) != 1:
        return None
    winner = next(iter(hits))
    matching = [c for c in candidates if (c.get("category") or "") == winner]
    return matching[0] if len(matching) == 1 else None


def score_by_photo(candidates: List[Dict[str, Any]],
                   vlm_facts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Match the form factor the vision pass reported against candidate categories."""
    for facts in vlm_facts or []:
        detected = facts.get("detected") or {}
        form = (detected.get("form_factor") or "").strip().lower()
        brand = (detected.get("brand") or "").strip().lower()
        if form:
            matching = [c for c in candidates if (c.get("category") or "").lower() == form]
            if len(matching) == 1:
                return matching[0]
        if brand:
            matching = [c for c in candidates if (c.get("brand") or "").lower() == brand]
            if len(matching) == 1:
                return matching[0]
    return None


async def disambiguate(
    mentions: List[str], text: str, customer_id: Optional[str] = None,
    vlm_facts: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], str]:
    """Resolve a product mention to one product, or report the real ambiguity.

    Returns `(resolved_or_none, candidates, how)`. `how` names the discriminator that
    won, which the composer turns into the "going by your March order…" clause.
    """
    candidates: List[Dict[str, Any]] = []
    seen = set()
    for mention in mentions or []:
        for row in await find_by_alias(mention):
            if row["product_id"] not in seen:
                seen.add(row["product_id"])
                candidates.append(row)

    if not candidates:
        return None, [], "no_match"
    if len(candidates) == 1:
        return candidates[0], candidates, "alias_unique"

    owned = await owned_products(customer_id, [c["product_id"] for c in candidates])
    if len(owned) == 1:
        match = next(c for c in candidates if c["product_id"] == owned[0])
        return match, candidates, "purchase_history"

    by_symptom = score_by_symptom(candidates, text)
    if by_symptom:
        return by_symptom, candidates, "symptom_vocabulary"

    by_photo = score_by_photo(candidates, vlm_facts or [])
    if by_photo:
        return by_photo, candidates, "photo"

    log.info("products.ambiguous", mentions=mentions, n=len(candidates))
    return None, candidates, "ambiguous"


async def find_error_code(code: str, product_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Exact lookup beats RAG for `E-05`, and it cannot hallucinate a meaning."""
    if product_id:
        rows = await db.fetch(
            """
            select ec.code, ec.meaning, ec.severity, ec.fix_steps, ec.source_url,
                   p.sku, p.name
            from error_codes ec join products p on p.id = ec.product_id
            where upper(ec.code) = upper(%s) and ec.product_id = %s
            """,
            (code, product_id),
        )
        if rows:
            return rows
    return await db.fetch(
        """
        select ec.code, ec.meaning, ec.severity, ec.fix_steps, ec.source_url, p.sku, p.name
        from error_codes ec join products p on p.id = ec.product_id
        where upper(ec.code) = upper(%s)
        limit 5
        """,
        (code,),
    )
