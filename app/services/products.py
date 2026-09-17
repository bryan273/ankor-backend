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
        where a.alias = %s and p.status <> 'invalid'
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
          and p.status <> 'invalid'
        order by a.confidence desc, p.name
        limit 12
        """,
        (alias,),
    )


async def facets() -> Dict[str, Any]:
    """Brand and category counts over the WHOLE catalog.

    The storefront cannot derive these from a page of results: a filter rail built from
    the 60 rows currently on screen shows "Cameras 3" for a catalog that holds 66, and
    hides every category that happens to fall below the fold. One cheap grouped query
    is the honest version.

    Uncategorised products are counted but not offered as a filter — a chip labelled
    `null` helps nobody, and a shopper who cannot find their device has search.
    """
    brands = await db.fetch(
        "select brand, count(*) as n from products where brand is not null "
        "group by brand order by n desc"
    )
    categories = await db.fetch(
        "select category, count(*) as n from products where category is not null "
        "group by category order by n desc"
    )
    totals = await db.fetch_one(
        "select count(*) as total, count(price) as priced, "
        "count(*) filter (where status = 'discontinued') as discontinued from products"
    )
    return {
        "brands": [{"value": r["brand"], "count": r["n"]} for r in brands],
        "categories": [{"value": r["category"], "count": r["n"]} for r in categories],
        "total": (totals or {}).get("total", 0),
        "priced": (totals or {}).get("priced", 0),
        "discontinued": (totals or {}).get("discontinued", 0),
    }


COLUMNS = ("p.id::text as product_id, p.sku, p.name, p.brand, p.category, p.price, "
           "p.currency, p.url, p.hero_image, p.status, p.warranty_months")

SIMPLE_ORDERS = {
    "price_asc": "p.price asc nulls last, p.name",
    "price_desc": "p.price desc nulls last, p.name",
    "name": "p.name",
}

# How an unsearched catalog page is ordered.
#
# Two obvious defaults are both wrong, and it took seeing them to know it. Alphabetical
# sorts names beginning with a digit to the top, so the shop opens on "10-Circuit Manual
# Transfer Switch" and three mounting brackets. Price-descending opens on six near
# identical $12,000 SOLIX bundles — the catalog's ceiling, not its range.
#
# So: take the best of each category in turn. Rank within each category (photographed
# first, then priciest), then read across those ranks — every category's flagship, then
# every category's runner-up. The first screen becomes a vacuum, a power station, a
# speaker, a camera, which is what a shelf looks like and what tells a customer what
# is actually sold here. Spare parts and gift cards sort behind all of it.
FEATURED_ORDER = (
    "(case when p.category in ('accessory', 'service') then 1 else 0 end), "
    "p.rank_in_category, p.price desc nulls last, p.name"
)
RANK_WINDOW = ("row_number() over (partition by p.category "
               "order by (p.hero_image is null), p.price desc nulls last, p.name)")


async def search_products(
    query: str = "", brand: Optional[str] = None, category: Optional[str] = None,
    limit: int = 12, sort: str = "",
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
    where = " and ".join(clauses)

    # An explicit sort always wins. Otherwise a search is ordered by how well the name
    # matches — which is the whole point of typing one — and a bare catalog page falls
    # back to `featured`.
    if sort in SIMPLE_ORDERS:
        sql = (f"select {COLUMNS} from products p where {where} "
               f"order by {SIMPLE_ORDERS[sort]} limit %s")
    elif query and sort != "featured":
        # Devices before their spare parts. Trigram similarity rewards short names, so
        # a search for "s1 pro" returned "Dust Bin For S1 Pro" and "Swivel Wheel For S1
        # Pro" ahead of the two actual S1 Pro products — the catalog answering a
        # question nobody asked. Somebody searching a model name wants the machine;
        # the replacement tank is still there, one screen down.
        sql = (f"select {COLUMNS} from products p where {where} "
               f"order by (case when p.category in ('accessory', 'service') "
               f"then 1 else 0 end), similarity(p.name, %s) desc, p.name limit %s")
        params.append(query)
    else:
        # Ranking has to happen inside the subquery: a window function cannot be used
        # in the ORDER BY of the select that computes it.
        sql = (f"select {COLUMNS} from ("
               f"  select *, {RANK_WINDOW} as rank_in_category from products p"
               f"  where {where}"
               f") p order by {FEATURED_ORDER} limit %s")
    params.append(limit)
    return await db.fetch(sql, params)


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


def category_from_symptom(text: str) -> Optional[str]:
    """Which product category the customer's own words point at, if exactly one.

    Two categories matching means the wording genuinely does not discriminate, and
    guessing there is how a breast-pump question gets answered with vacuum instructions.
    """
    if not text:
        return None
    lowered = text.lower()
    hits: Dict[str, int] = {}
    for category, signals in CATEGORY_SIGNALS.items():
        n = sum(1 for s in signals if s in lowered)
        if n:
            hits[category] = n
    return next(iter(hits)) if len(hits) == 1 else None


def narrow_by_symptom(candidates: List[Dict[str, Any]],
                      text: str) -> List[Dict[str, Any]]:
    """Drop candidates from categories the customer's words rule out.

    Narrowing rather than picking is the point. "How do I reset my Omni S1 Pro robot
    vacuum" names a category unambiguously but still matches two vacuum variants; the
    old version demanded a single survivor, found two, gave up, and asked "is this a
    robot vacuum or a breast pump?" — a question the customer had already answered in
    the same sentence. Narrowing first means any question that remains is the real one.
    """
    category = category_from_symptom(text)
    if not category:
        return candidates
    matching = [c for c in candidates if (c.get("category") or "") == category]
    return matching or candidates


def best_name_match(candidates: List[Dict[str, Any]],
                    mention: str) -> Optional[Dict[str, Any]]:
    """The candidate whose name actually contains what the customer typed.

    "Omni S1 Pro" matches both "…Omni S1 Pro" and "…Omni S1" through the alias table,
    but only one of them contains the whole phrase. Longest containment wins; a tie
    means the mention genuinely does not separate them.
    """
    needle = normalise_alias(mention)
    if not needle:
        return None
    # Token containment, not substring: storefront names interleave category words
    # ("eufy ROBOT VACUUM Omni S1 Pro"), so the customer's phrasing is rarely contiguous
    # inside them. Every word they typed must appear; order does not matter.
    wanted = set(needle.split())
    matching = [c for c in candidates
                if wanted <= set(normalise_alias(c.get("name", "")).split())]
    if len(matching) == 1:
        return matching[0]
    if not matching:
        return None

    # Several names contain the mention. Breaking that tie is only safe when they are
    # variants of the same thing — then the shortest name is the plain model and the
    # longer ones are bundles. Across categories it is never safe: "S1 Pro" is contained
    # by both the robot vacuum and the breast pump, and picking the shorter name would
    # silently answer a breast-pump question with vacuum instructions.
    categories = {c.get("category") for c in matching}
    if len(categories) > 1:
        return None
    return min(matching, key=lambda c: len(c.get("name", "")))


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


# What the vision pass calls a thing, and what the catalog calls it. Mostly identical,
# which is why this was never written down — until a photo-only message needed to turn
# "robot_vacuum" into a candidate list and found nothing to turn it with.
FORM_FACTOR_TO_CATEGORY = {
    "robot_vacuum": "robot_vacuum", "stick_vacuum": "stick_vacuum",
    "breast_pump": "breast_pump", "baby_monitor": "baby_monitor",
    "charger": "charger", "power_bank": "power_bank",
    "power_station": "power_station", "audio": "audio",
    "security_camera": "security_camera", "projector": "projector",
    "smart_lock": "smart_lock", "webcam": "webcam",
}


async def candidates_from_photo(
    vlm_facts: Optional[List[Dict[str, Any]]], customer_id: Optional[str] = None,
    limit: int = 4,
) -> Tuple[List[Dict[str, Any]], str]:
    """Candidate products for a photo that named no model, and how they were found.

    A customer who photographs their vacuum and says "this is my vacuum" gives us a
    category and nothing else, because the model number is on a sticker underneath. No
    amount of image similarity recovers a number that is not in the pixels — and these
    devices are near-identical across a range, so a visual match returns a confident
    WRONG model, which is worse than admitting we cannot tell.

    What actually narrows it is what the customer already told us elsewhere:

    1. **What they bought.** If we know who they are, their own devices in that category
       are the answer, and usually there is only one.
    2. **Failing that, the catalog** for that category — offered as something to tap,
       not as a question to type an answer to.
    """
    for facts in vlm_facts or []:
        detected = facts.get("detected") or {}
        form = (detected.get("form_factor") or "").strip().lower()
        category = FORM_FACTOR_TO_CATEGORY.get(form)
        if not category:
            continue

        if customer_id:
            owned = await db.fetch(
                """
                select distinct p.id::text as product_id, p.sku, p.name, p.brand,
                       p.category, p.price, p.hero_image
                from orders o
                join order_items oi on oi.order_id = o.id
                join products p on p.id = oi.product_id
                where o.customer_id = %s and p.category = %s
                order by p.name
                limit %s
                """,
                (customer_id, category, limit),
            )
            if owned:
                return owned, "photo_and_purchase_history"

        brand = (detected.get("brand") or "").strip().lower()
        # Bundles last. The catalog is full of "X10 Pro Omni + Dust Bags (6-Pack)" and
        # "SoloCam S340 + eufy X10 Pro Omni", and sorting by price put three of those in
        # a four-option picker — nobody looking at their own vacuum thinks "mine is the
        # one that came with six dust bags". The device itself is what they recognise,
        # and its bundles resolve to the same support answer anyway.
        rows = await db.fetch(
            """
            select p.id::text as product_id, p.sku, p.name, p.brand, p.category,
                   p.price, p.hero_image
            from products p
            where p.category = %s
              and p.hero_image is not null
              and p.status not in ('discontinued', 'invalid')
              and (%s = '' or lower(p.brand) = %s)
            order by (p.name like '%%+%%' or p.sku ilike 'BUNDLE-%%'),
                     p.price desc nulls last, p.name
            limit %s
            """,
            (category, brand, brand, limit),
        )
        if rows:
            return rows, "photo_category"
    return [], "no_match"


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

    # Narrow before deciding. Each discriminator removes candidates it rules out, so a
    # question that survives to the end is a question the customer has genuinely not
    # answered — rather than one they answered in the same sentence.
    narrowed = narrow_by_symptom(candidates, text)
    if len(narrowed) == 1:
        return narrowed[0], candidates, "symptom_vocabulary"

    by_photo = score_by_photo(narrowed, vlm_facts or [])
    if by_photo:
        return by_photo, candidates, "photo"

    exact = best_name_match(narrowed, mentions[0] if mentions else "")
    if exact:
        return exact, candidates, "exact_name"

    # Ask, but only among the candidates still standing.
    log.info("products.ambiguous", mentions=mentions, n=len(narrowed),
             narrowed_from=len(candidates))
    return None, narrowed, "ambiguous"


# A row earns a place in a lookup result only if it can say something: a meaning, or
# steps. The scraped import added 66 codes with `meaning` deliberately NULL (the scrape
# never carried one) and 26 of those have no steps either — nothing but a code. Those
# still answered "found", which is the expensive kind of wrong: the composer is told the
# code is recognised and has nothing to state, and the handoff briefing rendered the
# literal sentence "Error C1 means: None" to a human agent.
_HAS_SOMETHING_TO_SAY = """
    (ec.meaning is not null
     or case when jsonb_typeof(ec.fix_steps) = 'array'
             then jsonb_array_length(ec.fix_steps) else 0 end > 0)
    and coalesce(ec.provenance, '') <> 'scraped_unattributed'
"""

# `scraped_unattributed` is the second half of the same problem, and having steps is not
# enough to be worth serving. 33 rows carry one identical 12-step list stamped onto every
# code named in `S1-Pro-Common-Voice-Errors-and-Basic-Troubleshooting-Guide` — and the
# extractor filed them against the eufy Wearable Breast Pump S1 Pro, because "S1 Pro" is
# also the Robot Vacuum Omni S1 Pro. So E72 on a breast pump answered "turn the robot
# over and clean the brush slot", from the deterministic tool the agent trusts above RAG.
# See scripts/audit_error_codes.py.

# One code, one answer. E-05 is stored seven times — once per product it can appear on,
# and six of those products are spare parts (bumpers, brush guards, a wheel) whose names
# match the vacuum they fit. Same meaning, same steps, seven rows, so `matches[:3]` was
# three copies of one fact labelled with three accessories. Collapsing on the CONTENT
# and preferring a real device keeps the answer and drops the noise.
_ERROR_CODE_SELECT = f"""
    select distinct on (coalesce(ec.meaning, ''), coalesce(ec.fix_steps::text, ''))
           ec.code, ec.meaning, ec.severity, ec.fix_steps, ec.source_url, p.sku, p.name
    from error_codes ec
    left join products p on p.id = ec.product_id
    where upper(ec.code) = upper(%s) and {_HAS_SOMETHING_TO_SAY}
    {{extra}}
    order by coalesce(ec.meaning, ''), coalesce(ec.fix_steps::text, ''),
             (p.category in ('accessory', 'service')) nulls last,
             p.name
"""


async def find_error_code(code: str, product_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Exact lookup beats RAG for `E-05`, and it cannot hallucinate a meaning.

    The join is LEFT, not inner: the import resolved six codes to no product at all, and
    an inner join silently dropped them even though they carry usable repair steps.
    """
    if product_id:
        rows = await db.fetch(
            _ERROR_CODE_SELECT.format(extra="and ec.product_id = %s"),
            (code, product_id),
        )
        if rows:
            return rows
    return (await db.fetch(_ERROR_CODE_SELECT.format(extra=""), (code,)))[:5]
