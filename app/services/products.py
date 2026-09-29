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
    "power_station": ["power station", "powerhouse", "solix", "solar", "camping", "generator",
                      "inverter", "blackout", "kwh"],
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
    "price_asc": "(p.currency <> 'USD'), p.price asc nulls last, p.name",
    "price_desc": "(p.currency <> 'USD'), p.price desc nulls last, p.name",
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
# A card with no photograph is a grey box with the brand name printed in it, and four
# of those on the first screen make the shop look broken. 599 of 1,491 rows have no
# image, so "photographed first" is a global key, ahead of the category interleave —
# every category still gets its turn, just with something to look at.
FEATURED_ORDER = (
    "(p.hero_image is null), (p.price is null), "
    "(case when p.category in ('accessory', 'service') then 1 else 0 end), "
    "p.rank_in_category, p.price desc nulls last, p.name"
)
RANK_WINDOW = ("row_number() over (partition by p.category "
               "order by (p.hero_image is null), p.price desc nulls last, p.name)")


# The same model is listed once per storefront: 1,493 rows collapse to 1,364 models,
# and 113 of them carry two or three listings that differ only in currency. Nothing
# collapsed them, so a search for "charger" answered with the Anker 323 Charger (33W)
# three times over, at 19.99, 27.99 and 39.99 — which reads as three products until you
# notice those are USD, EUR and AUD. `canonical_id` was written for exactly this and was
# never read at query time.
#
# One listing per model, then: the dollar one where it exists, because the storefront
# and the cards print dollars, then the one with a photograph, then the cheapest.
VARIANT_PICK = ("p.canonical_id, (p.currency <> 'USD'), (p.hero_image is null), "
                "p.price asc nulls last, p.sku")

# Words that match everything and therefore rank nothing.
STOPWORDS = frozenset((
    "the", "for", "with", "and", "any", "one", "that", "this", "you", "your", "our",
    "please", "recommend", "need", "want", "buy", "sell", "best", "good", "get",
    "have", "looking", "something", "anything", "there", "about", "does", "did",
))


# How rare a word has to be before it counts as the point of the query. Tuned against
# the catalogue: "charging" is in 300-odd names and says almost nothing, "iphone" is in
# none and says everything.
DISTINCTIVE_DF = 120
_DF: Dict[str, int] = {}
_ROWS: Optional[int] = None


WORD = r"\y{}\y"  # Postgres word boundary: "trip" must not match "Power Strip".


def _forms(word: str) -> List[str]:
    """A word and the form the catalogue is likely to spell it in.

    Shoppers type plurals and product names do not: 29 names contain "robot", three
    contain "vacuums", and all three of those are mop cloths compatible with robot
    vacuums. Matching only what was typed answered "im looking at robot vacuums" with
    replacement pads.
    """
    forms = [word]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        forms.append(word[:-1])
        if word.endswith("es") and len(word) > 4:
            forms.append(word[:-2])
    return forms


def _pattern(word: str) -> str:
    return r"\y(" + "|".join(_forms(word)) + r")\y"


async def _doc_freq(word: str) -> int:
    """How many product names contain this word. Cached: the catalogue does not move."""
    if word not in _DF:
        rows = await db.fetch(
            "select count(*) c from products where status <> 'invalid' and name ~* %s",
            [_pattern(word)])
        _DF[word] = int(rows[0]["c"]) if rows else 0
    return _DF[word]


async def _really_absent(word: str) -> bool:
    """A word no name contains — is that a typo, or a thing this shop does not sell?

    Measured against this catalogue: misspellings score 0.57 and up ("vacuuum" 0.88,
    "chargr" 0.71, "powerbnk" 0.67, "earbds" 0.57) while things nobody here sells score
    0.43 and below ("fridge" 0.43, "dyson" 0.33, "samsung" 0.25). One exception breaks
    the rule and it is the most likely typo of all: "anekr" scores 0.33, because trigram
    similarity is blind to transposed letters. Brands are four known names, so that one
    is worth checking directly rather than guessing at a lower threshold that would let
    "dyson" through.
    """
    rows = await db.fetch(
        "select max(word_similarity(%s, name)) m from products where status <> 'invalid'",
        [_forms(word)[-1]])
    if float((rows[0]["m"] if rows else None) or 0) >= 0.50:
        return False
    brands = await db.fetch(
        "select 1 from products where status <> 'invalid' "
        "and word_similarity(%s, brand) >= 0.3 limit 1", [word])
    return not brands


def _terms(query: str) -> Tuple[List[str], List[str]]:
    """The words worth scoring, and the adjacent pairs of them.

    Whole-string trigram similarity is the wrong instrument for a shopping query: it
    rewards short names, so "fast charging power bank" ranked a Fast Charging Power
    Strip above every power bank in the catalogue. Counting which words of the query
    appear in the name, and paying double when two of them appear *together*, is closer
    to what the customer meant by putting them in that order.
    """
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower())
             if w not in STOPWORDS
             and (len(w) > 2 or (len(w) > 1 and any(ch.isdigit() for ch in w)))]
    pairs = [f"{a} {b}" for a, b in zip(words, words[1:])]
    return words, pairs


async def search_products(
    query: str = "", brand: Optional[str] = None, category: Optional[str] = None,
    limit: int = 12, sort: str = "",
) -> List[Dict[str, Any]]:
    """Name search over one listing per model — the catalog page and `search_products`.

    The matching is deliberately word-led rather than phrase-led. A shopper types a
    sentence, not a product name: "fast charging power bank" appears verbatim in nothing,
    so a whole-string match finds nothing and whole-string trigram similarity finds
    whatever is shortest. What decides the result here is the RAREST word in the query,
    because that is the one carrying the request — "bank" in that sentence, "iphone" in
    "iphone case", "s1" in "s1 pro" — while "fast" and "charging" say almost nothing.
    """
    # `invalid` is the quarantine flag for rows that are not products: a promo tile
    # ("Up to $850 off"), a bare variant id.
    clauses, params = ["p.status <> 'invalid'"], []

    words: List[str] = []
    pairs: List[str] = []
    weights: Dict[str, int] = {}
    rarest = ""
    if query:
        words, pairs = _terms(query)
        weights = {w: await _doc_freq(w) for w in words}
        absent = [w for w, df in weights.items() if df == 0 and await _really_absent(w)]
        # An absent word only ends the search when it IS the request. "do you sell
        # iphones" asks for something this shop does not stock, and six near-misses
        # presented as an answer is worse than an empty shelf. But "robot vacuum, i have
        # a dog and wooden floors" also contains words no product name carries, and
        # answering that with nothing is worse still — the dog is a constraint, not the
        # thing being bought.
        if absent and (words[-1] in absent or len(absent) == len(words)):
            log.info("products.absent_term", query=query[:60], word=absent[0])
            return []
        for w in [w for w, df in weights.items() if df == 0]:
            weights.pop(w, None)
        words = [w for w in words if w in weights]
        heads = [w for w in reversed(words) if weights[w] <= DISTINCTIVE_DF]
        head = heads[0] if heads else ""
        if head:
            clauses.append("p.name ~* %s")
            params.append(_pattern(head))
        else:
            # Nothing in the query is rare enough to narrow on ("charger", "cable").
            # Fall back to the phrase, which is what a one-word query is anyway.
            clauses.append(
                "(p.name ilike %s or p.sku ilike %s or similarity(p.name, %s) > 0.2)")
            params += [f"%{query}%", f"%{query}%", query]
    if brand:
        clauses.append("p.brand ilike %s")
        params.append(brand)
    if category:
        clauses.append("p.category = %s")
        params.append(category)
    where = " and ".join(clauses)

    # Collapse the storefront duplicates first, so every ordering below ranks models
    # rather than listings, and `limit` counts products the customer could tell apart.
    # `canonical_id` catches the variants it was built for; identical names catch the
    # rows that were never linked, and three identical cards are three identical cards
    # whatever the ids say.
    by_canonical = (f"select distinct on (p.canonical_id) {COLUMNS} "
                    f"from products p where {where} order by {VARIANT_PICK}")
    deduped = (f"select distinct on (p.brand, lower(p.name)) * from ({by_canonical}) p "
               f"order by p.brand, lower(p.name), (p.currency <> 'USD'), "
               f"(p.hero_image is null), p.price asc nulls last, p.sku")

    if sort in SIMPLE_ORDERS:
        sql = f"select * from ({deduped}) p order by {SIMPLE_ORDERS[sort]} limit %s"
    # `words or pairs` is a guard, not an optimisation. `_terms` only sees [a-z0-9], so a
    # query with no ASCII word in it tokenises to nothing: every Chinese query does, and so
    # does an English one made entirely of stopwords ("best", "the best one"). The score
    # below is then the join of a single "0", and `order by (0) desc` is not a constant in
    # Postgres — a bare integer there is an ordinal, so it raises InvalidColumnReference:
    # ORDER BY position 0 is not in select list, and the whole turn 500s. Scoring by term
    # overlap is meaningless with no terms anyway, so fall through to the featured shelf
    # and let the caller's vector fallback handle what a name match cannot reach.
    elif query and (words or pairs) and sort != "featured":
        # Devices before their spare parts, and the thing itself before a bundle of it
        # with two others. Trigram similarity rewards short names, so a search for "s1
        # pro" put "Dust Bin For S1 Pro" above both actual S1 Pro products.
        def weight(w: str) -> int:
            df = weights.get(w) or 1
            return 6 if df <= 20 else 4 if df <= DISTINCTIVE_DF else 1

        score = " + ".join(["0"] + [f"(p.name ilike %s)::int * 3" for _ in pairs]
                           + [f"(p.name ~* %s)::int * {weight(w)}" for w in words])
        params += ([f"%{t}%" for t in pairs] + [_pattern(w) for w in words] + [query])
        sql = (f"select * from ({deduped}) p "
               f"order by (case when p.category in ('accessory', 'service') "
               f"then 1 else 0 end), "
               f"(case when p.sku ilike 'BUNDLE-%%' or p.name like '%%+%%' "
               # The storefront quotes dollars, so a model listed only in euros or
               # Australian dollars sorts behind the ones a customer here can actually
               # compare. "The cheapest one that empties itself" came back as A$1,529.99
               # ranked against US prices, which is not a comparison.
               f"then 1 else 0 end), (p.currency <> 'USD'), "
               f"({score}) desc, similarity(p.name, %s) desc, "
               f"(p.hero_image is null), (p.price is null), p.name limit %s")
    else:
        # Ranking has to happen inside the subquery: a window function cannot be used
        # in the ORDER BY of the select that computes it.
        sql = (f"select * from ("
               f"  select p.*, {RANK_WINDOW} as rank_in_category from ({deduped}) p"
               f") p order by {FEATURED_ORDER} limit %s")
    params.append(limit)
    rows = await db.fetch(sql, params)

    # A head word that narrows to nothing was the wrong head word, not an empty shelf.
    # Try the next candidate before concluding the catalogue has no answer.
    if not rows and len(heads) > 1 and not (brand or category):
        return await search_products(" ".join(w for w in words if w != head),
                                     brand, category, limit, sort)
    return rows


async def by_skus(skus: List[str]) -> List[Dict[str, Any]]:
    """Real catalogue rows for a list of SKUs, collapsed and in the order given.

    The vector fallback returns Pinecone metadata, which never passed through any of the
    collapsing above: it brought back the same model in two currencies, rows with no
    price, and bundles ahead of the thing itself. Every route to a product card has to
    leave by the same door, so the fallback's SKUs are looked up here instead.
    """
    if not skus:
        return []
    rows = await db.fetch(
        f"select {COLUMNS}, p.canonical_id from products p "
        f"where p.sku = any(%s) and p.status <> 'invalid'", [list(skus)])
    order = {s: i for i, s in enumerate(skus)}
    rows.sort(key=lambda r: order.get(r["sku"], 999))
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for r in rows:
        key = (r.get("canonical_id"), (r.get("name") or "").strip().lower())
        if key[0] in seen or key[1] in seen:
            continue
        seen.add(key[0])
        seen.add(key[1])
        out.append(r)
    out.sort(key=lambda r: ((r.get("sku") or "").upper().startswith("BUNDLE-")
                            or " + " in (r.get("name") or ""),
                            r.get("price") is None,
                            order.get(r["sku"], 999)))
    return out


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


async def owned_in_category(customer_id: Optional[str], text: str) -> Optional[Dict[str, Any]]:
    """The one device of this kind the signed-in customer owns, if exactly one.

    "My power station won't charge" names no model, and the alias table has no entry for
    a phrase that generic, so disambiguation used to end at no_match. The customer had
    bought exactly one power station, and the agent answered from the F2000 manual and
    asked "which model do you have?". Their orders already said.

    Spare parts, accessories and gift cards are not the device the customer means:
    Nadia's account holds a SOLIX gift card filed under power_station.
    """
    category = category_from_symptom(text)
    if not customer_id or not category:
        return None
    rows = await db.fetch(
        """
        select distinct p.id::text as product_id, p.sku, p.name, p.brand, p.category,
               p.hero_image, p.price, p.status
        from order_items oi
        join orders o on o.id = oi.order_id
        join products p on p.id = oi.product_id
        where o.customer_id = %s and p.category = %s and p.status <> 'invalid'
          and p.name !~* '(gift card|replacement|spare|filter|brush|bag|cable|case|ear tips|mount)'
        """,
        (customer_id, category),
    )
    if len(rows) == 1:
        log.info("products.owned_in_category", category=category, sku=rows[0]["sku"])
        return rows[0]
    return None


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


def _has_model_token(mention: str) -> bool:
    """A model name carries an identifier — `S1 Pro`, `X10`, `535`, `F3800`. A bare
    category ("power bank", "earbuds", "my charger") does not, and has no single answer."""
    return bool(re.search(r"\d", mention or ""))


async def disambiguate(
    mentions: List[str], text: str, customer_id: Optional[str] = None,
    vlm_facts: Optional[List[Dict[str, Any]]] = None, current: str = "",
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], str]:
    """Resolve a product mention to one product, or report the real ambiguity.

    Returns `(resolved_or_none, candidates, how)`. `how` names the discriminator that
    won, which the composer turns into the "going by your March order…" clause.
    """
    # One picker answers ONE question. Perception reads the rewritten query, which folds
    # in earlier turns, so "my power bank gets hot" after a chat about earbuds arrived as
    # two mentions — and their candidates were pooled into one picker titled "Which
    # soundcore earbuds do you have?" listing power-bank cables. What the customer typed
    # just now decides which mention is the question.
    if current and len(mentions or []) > 1:
        here = normalise_alias(current)
        now = [m for m in mentions if normalise_alias(m) and normalise_alias(m) in here]
        mentions = now or mentions

    candidates: List[Dict[str, Any]] = []
    seen = set()
    for mention in mentions or []:
        for row in await find_by_alias(mention):
            if row["product_id"] not in seen:
                seen.add(row["product_id"])
                candidates.append(row)

    if not candidates:
        owned = await owned_in_category(customer_id, " ".join(mentions or []) + " " + current)
        if owned:
            return owned, [owned], "purchase_history"
        # The alias table holds model names, not category words: "robot vacuum" matches
        # nothing in it, and nothing in it is what the caller got. So the shop answered
        # "my robot vacuum is showing an error code" with "which eufy or Anker model is
        # it?" while selling twenty-three of them and being perfectly able to show four.
        #
        # The catalogue search already knows how to turn a category phrase into the
        # products in it, so ask it, and offer the result as something to point at. Only
        # when the answers agree on what kind of thing they are: a picker that mixes
        # categories reads as the agent having understood nothing.
        phrase = " ".join(mentions or []).strip() or current
        guess = await search_products(phrase, limit=6) if phrase else []
        kinds = {g.get("category") for g in guess if g.get("category")}
        if len(guess) > 1 and len(kinds) == 1:
            log.info("products.category_picker", phrase=phrase[:50], n=len(guess))
            return None, guess[:4], "generic_category"
        return None, [], "no_match"

    owned = await owned_products(customer_id, [c["product_id"] for c in candidates])
    if len(owned) == 1:
        match = next(c for c in candidates if c["product_id"] == owned[0])
        return match, candidates, "purchase_history"

    # A generic phrase names a category, not a product. The alias table maps "anker
    # power bank" to exactly one SKU, so it resolved "uniquely" to the 10K Fusion — which
    # was then remembered, and three turns later "back to the earbuds" was answered with
    # "the only device linked to you is the Power Bank (10K, Fusion)". Only the
    # customer's own purchase (above) may pin a SKU from a generic phrase.
    if not any(_has_model_token(m) for m in mentions or []):
        log.info("products.generic_mention", mentions=mentions, n=len(candidates))
        owned = await owned_in_category(customer_id, " ".join(mentions or []) + " " + current)
        if owned:
            return owned, [owned], "purchase_history"
        # Refusing to PIN a SKU from a generic phrase is right. Returning nothing at all
        # was not: the caller reads an empty candidate list as "no idea what they mean"
        # and falls back to asking in prose, so "my robot vacuum is showing an error
        # code" got "which eufy or Anker model is it?" while the shop sells twenty-three
        # of them and could simply have shown four. Hand the candidates back unresolved
        # so the customer can point at one.
        #
        # Only when they are all the same kind of thing. A picker that mixes categories
        # is the failure this branch exists to prevent, and it reads as the agent having
        # understood nothing.
        kinds = {c.get("category") for c in candidates if c.get("category")}
        if len(kinds) == 1 and len(candidates) > 1:
            return None, candidates[:4], "generic_category"
        return None, [], "generic"

    if len(candidates) == 1:
        return candidates[0], candidates, "alias_unique"

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

# A code is matched on its SHAPE, not its spelling. The seeded rows are written `E-05`,
# but the robot's screen, the eufy app and every customer write `E05` — and the photo
# path reads the screen verbatim. An exact compare made the headline S1 demo (an angry
# customer photographing the app error) answer "I don't have anything on file for E05"
# while the fix sat in the table under a dash. Normalise both sides the same way:
# uppercase, drop separators, drop leading zeros after the letter prefix
# (`e-05` = `E05` = `E5`; `EB01` = `EB1`; `E10` stays `E10`).
_NORM_SQL = ("regexp_replace(regexp_replace(upper(ec.code), '[^A-Z0-9]', '', 'g'), "
             "'([A-Z])0+([0-9])', '\\1\\2', 'g')")


def normalise_code(code: str) -> str:
    s = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
    return re.sub(r"([A-Z])0+(\d)", r"\1\2", s)


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
    where {_NORM_SQL} = %s and {_HAS_SOMETHING_TO_SAY}
    {{extra}}
    order by coalesce(ec.meaning, ''), coalesce(ec.fix_steps::text, ''),
             (p.category in ('accessory', 'service')) nulls last,
             p.name
"""


async def find_error_code(code: str, product_id: Optional[str] = None,
                          category: Optional[str] = None) -> List[Dict[str, Any]]:
    """Exact lookup beats RAG for `E-05`, and it cannot hallucinate a meaning.

    The join is LEFT, not inner: the import resolved six codes to no product at all, and
    an inner join silently dropped them even though they carry usable repair steps.
    """
    if product_id:
        rows = await db.fetch(
            _ERROR_CODE_SELECT.format(extra="and ec.product_id = %s"),
            (normalise_code(code), product_id),
        )
        if rows:
            return rows
    # Fall back across the catalogue — but never across KINDS of device. A breast pump
    # with no E01 of its own was handed the robot vacuum's "E01: left wheel jammed".
    if category:
        rows = await db.fetch(_ERROR_CODE_SELECT.format(extra="and p.category = %s"),
                              (normalise_code(code), category))
        return rows[:5]
    return (await db.fetch(_ERROR_CODE_SELECT.format(extra=""), (normalise_code(code),)))[:5]
