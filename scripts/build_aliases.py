"""Build the product alias table — the deterministic half of disambiguation.

Customers do not type "eufy Robot Vacuum Omni S1 Pro". They type "S1 Pro", "my omni",
"the s1". Each of those has to reach the right product without a model call, and when a
name genuinely belongs to two products the table has to *say so* rather than pick one.

That last part is scenario S2 and this script asserts it: `"s1 pro"` must map to at
least two products in different categories. If it does not, the crawl missed a
sub-brand and the headline demo has quietly become an ordinary lookup — better to fail
here, loudly, than to discover it on stage.

    python scripts/build_aliases.py
    python scripts/build_aliases.py --check   # assert only, change nothing
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sys
from typing import Dict, List, Set, Tuple

import structlog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402
from app.services.products import normalise_alias  # noqa: E402

log = structlog.get_logger("aliases")

# Words that carry no identifying power. An alias of "robot" would match everything.
STOP = {
    "eufy", "anker", "soundcore", "nebula", "ankerwork", "solix", "baby", "the", "with",
    "and", "for", "new", "pro", "plus", "max", "mini", "lite", "series", "edition",
    "bundle", "kit", "pack", "set", "us", "eu", "uk", "black", "white", "grey", "gray",
    "blue", "green", "red", "gb", "tb", "inch", "cm", "mm",
}

GENERIC = {
    "robot vacuum", "vacuum", "breast pump", "pump", "charger", "cable", "power bank",
    "earbuds", "headphones", "speaker", "camera", "doorbell", "projector", "power station",
    "smart lock", "webcam", "monitor", "printer", "mower", "battery",
}

# Model designators: the part customers actually say. "S1 Pro", "X10", "C1000", "L60".
MODEL_RE = re.compile(
    r"\b([A-Z]{1,3}\s?\d{1,4}[A-Z]?(?:\s+(?:Pro|Plus|Max|Ultra|Lite|Omni|SE))*)\b",
    re.IGNORECASE)


# Spare parts and accessories carry the parent product's model name — "Dust Bin For S1
# Pro", "Filter Tray For S1 Pro and Omni S1" — so without this filter the disambiguation
# picker offers a customer their own dustbin as a candidate device. Accessories stay in
# the catalog and stay searchable; they just do not compete for a model-name alias.
ACCESSORY_RE = re.compile(
    r"\b(for\s+(?:the\s+)?[a-z0-9]|replacement|spare|refill|filter tray|dust\s?bin|"
    r"brush guard|water tank|reservoir|swivel wheel|magnetic cover|cover for|mop pad|"
    r"side brush|roller brush|duckbill|diaphragm|flange|valve|adapter|charging dock only|"
    r"2-pack|4-pack|6-pack|accessor(?:y|ies)|cleaning kit|detergent|tablets)\b",
    re.IGNORECASE)

# Variant SKUs (T2080111-82) are parts of a parent SKU (T2080111).
VARIANT_SKU_RE = re.compile(r"^[A-Z]?\d{3,}[A-Z0-9]*-\d{2,}$", re.IGNORECASE)


def is_accessory(name: str, sku: str = "") -> bool:
    return bool(ACCESSORY_RE.search(name or "")) or bool(VARIANT_SKU_RE.match(sku or ""))


def candidate_aliases(name: str) -> Set[str]:
    """Every phrase a customer might plausibly use for this product."""
    out: Set[str] = set()
    clean = re.sub(r"\s*[|(].*$", "", name).strip()
    out.add(clean)

    for match in MODEL_RE.finditer(clean):
        token = match.group(1).strip()
        if len(token) >= 2 and not token.lower() in STOP:
            out.add(token)
            # "S1 Pro" is what people say; "S1" is what they say when they are being brief.
            base = re.sub(r"\s+(pro|plus|max|ultra|lite|se)$", "", token, flags=re.IGNORECASE)
            if base != token and len(base) >= 2:
                out.add(base)

    # The distinctive tail of the name, minus brand noise: "Omni S1 Pro" from
    # "eufy Robot Vacuum Omni S1 Pro".
    words = [w for w in re.split(r"[\s\-—|]+", clean) if w]
    meaningful = [w for w in words if w.lower() not in STOP and len(w) > 1]
    if 2 <= len(meaningful) <= 5:
        out.add(" ".join(meaningful))
    if len(meaningful) >= 2:
        out.add(" ".join(meaningful[-2:]))

    normalised: Set[str] = set()
    for alias in out:
        n = normalise_alias(alias)
        # Two characters is noise; a bare generic term matches half the catalog.
        if len(n) >= 3 and n not in GENERIC and not n.isdigit():
            normalised.add(n)
    return normalised


async def build() -> Tuple[int, int]:
    rows = await db.fetch("select id::text as id, sku, name, brand, category from products")
    devices = [r for r in rows if not is_accessory(r["name"], r["sku"])]
    log.info("aliases.products", total=len(rows), devices=len(devices),
             accessories=len(rows) - len(devices))

    by_alias: Dict[str, List[str]] = {}
    for row in devices:
        for alias in candidate_aliases(row["name"]):
            by_alias.setdefault(alias, []).append(row["id"])

    # An alias matching a large slice of the catalog is a category word we failed to
    # filter, not a model name. Dropping it prevents "pump" resolving to 40 products.
    # A real model name points at a handful of variants; anything matching more than
    # this is a category word that slipped past the stop list. Scaling the cutoff with
    # catalog size was wrong — it let 30-way matches through at 800 products.
    cutoff = 8
    dropped = {a for a, ids in by_alias.items() if len(ids) > cutoff}
    for alias in dropped:
        del by_alias[alias]
    if dropped:
        log.info("aliases.dropped_too_broad", n=len(dropped),
                 examples=sorted(dropped)[:8])

    pairs: List[Tuple[str, str, float]] = []
    for alias, ids in by_alias.items():
        # An alias pointing at one product is a confident match; one pointing at several
        # is a genuine ambiguity, and the confidence says so.
        confidence = 1.0 if len(ids) == 1 else 0.6
        for pid in ids:
            pairs.append((alias, pid, confidence))

    await db.execute("delete from product_aliases where source = 'derived'")
    await db.execute_many(
        """
        insert into product_aliases (alias, product_id, confidence, source)
        values (%s, %s, %s, 'derived')
        on conflict (alias, product_id) do update set confidence = excluded.confidence
        """,
        pairs,
    )
    log.info("aliases.written", aliases=len(by_alias), pairs=len(pairs))
    return len(by_alias), len(pairs)


async def check() -> bool:
    """Scenario S2's precondition, asserted."""
    ok = True
    rows = await db.fetch(
        """
        select a.alias, p.sku, p.name, p.brand, p.category
        from product_aliases a join products p on p.id = a.product_id
        where a.alias = 's1 pro' order by p.category
        """
    )
    print("\n's1 pro' resolves to:")
    for r in rows:
        print(f"  {r['brand']:8} {r['sku']:16} {(r['category'] or '?'):16} {r['name'][:60]}")
    categories = {r["category"] for r in rows if r["category"]}
    if len(rows) < 2:
        print(f"\nFAIL: 's1 pro' maps to {len(rows)} product(s); scenario S2 needs >= 2.")
        ok = False
    elif len(categories) < 2:
        print(f"\nFAIL: 's1 pro' maps to {len(rows)} products but only one category "
              f"({categories}). The ambiguity must cross categories.")
        ok = False
    else:
        print(f"\nPASS: 's1 pro' is ambiguous across {len(categories)} categories "
              f"{sorted(categories)} — scenario S2 is live.")

    stats = await db.fetch_one(
        """
        select count(*) as total, count(distinct alias) as aliases,
               count(distinct product_id) as products
        from product_aliases
        """
    )
    print(f"alias table: {stats['aliases']} aliases, {stats['products']} products covered, "
          f"{stats['total']} pairs")

    ambiguous = await db.fetch(
        """
        select alias, count(*) as n from product_aliases group by alias
        having count(*) > 1 order by n desc, alias limit 10
        """
    )
    if ambiguous:
        print("\nmost ambiguous aliases (these drive the picker):")
        for a in ambiguous:
            print(f"  {a['alias']:28} {a['n']}")
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="assert only, do not rebuild")
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(processors=[structlog.processors.add_log_level,
                                    structlog.dev.ConsoleRenderer(colors=False)])

    await db.init_pool()
    try:
        if not args.check:
            await build()
        ok = await check()
    finally:
        await db.close_pool()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
