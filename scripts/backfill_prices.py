"""Fill in the prices the product crawl could not read.

Three of the four brands publish a price in their JSON-LD `Product` markup. soundcore
does not: its storefront is a headless Next.js front end over Shopify, and the only
thing its JSON-LD offers carry is a `9999999.99` out-of-stock sentinel. The real prices
travel in the RSC payload as `"price":{"amount":129.99}`, hanging off the product's
`handle`.

So we read them where they actually live, cheapest source first:

1. **Collection pages.** One request returns a `products[]` array with a handle and a
   price for ~200 products at a time. This covers the catalog proper in a handful of
   requests.
2. **The product's own page**, for what the collections miss — accessories, refurbished
   units, care plans. Here the handle appears in its own page, so we read the nearest
   price to it rather than the first price on the page, which would otherwise pick up a
   recommendation carousel and quietly price a charging case at the flagship's price.

A product that stays unpriced after both passes is almost always genuinely unbuyable
(discontinued or sold out), and NULL is the honest answer for it — better than a
sentinel that would make it the most expensive thing in the catalog.

    python scripts/backfill_prices.py --brand soundcore
    python scripts/backfill_prices.py --brand soundcore --skip-products   # pass 1 only
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sys
from typing import Dict, List, Optional

import structlog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402  — event-loop policy on Windows
from app.clients import db  # noqa: E402
from scripts.crawl_products import Crawler  # noqa: E402

log = structlog.get_logger("backfill")

# Handles are read out of a JSON blob, so the price we want sits a little after the
# handle, never before it. 700 characters covers the fields Shopify puts in between
# (title, image, vendor, availability) without reaching the next product in the array.
HANDLE_RE = re.compile(r'"handle":"([A-Za-z0-9][A-Za-z0-9_-]{2,})"')
PRICE_RE = re.compile(r'"price":\{"amount":"?([0-9]+(?:\.[0-9]+)?)"?')
LD_PRICE_RE = re.compile(r'"price":\s*"?([0-9]+(?:\.[0-9]+)?)"?')
LOOKAHEAD = 700

COLLECTIONS: Dict[str, List[str]] = {
    "soundcore": [
        "https://www.soundcore.com/collections/all-products",
        "https://www.soundcore.com/collections/all",
        "https://www.soundcore.com/collections/refurbished-products",
        "https://www.soundcore.com/collections/headphone-accessories",
        "https://www.soundcore.com/collections/speakers",
        "https://www.soundcore.com/collections/headphones",
        "https://www.soundcore.com/collections/true-wireless-earbuds",
        "https://www.soundcore.com/collections/sleep-earbuds",
        "https://www.soundcore.com/collections/open-ear-headphones",
        "https://www.soundcore.com/collections/noise-cancelling-headphones",
    ],
}


def sane(amount: str) -> Optional[float]:
    """A price that is not a sentinel. Shopify publishes out-of-stock variants at
    9999999.99, which sorts to the top of any catalog as the most expensive thing the
    brand sells."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    return value if 0 < value < 100_000 else None


def price_index(html: str) -> Dict[str, float]:
    """Every handle on the page, with the price that follows it."""
    found: Dict[str, float] = {}
    for m in HANDLE_RE.finditer(html):
        pm = PRICE_RE.search(html, m.end(), m.end() + LOOKAHEAD)
        if not pm:
            continue
        value = sane(pm.group(1))
        if value is not None:
            found.setdefault(m.group(1).lower(), value)
    return found


def handle_of(url: Optional[str]) -> str:
    return (url or "").rstrip("/").split("/")[-1].split("?")[0].lower()


def price_for_handle(html: str, handle: str) -> Optional[float]:
    """The price nearest THIS product's handle on its own page.

    Taking the first price on the page instead would read the recommendation carousel,
    which is rendered above the fold and belongs to a different product entirely.
    """
    pattern = re.compile(r'"(?:handle|slug)":"' + re.escape(handle) + r'"', re.IGNORECASE)
    for m in pattern.finditer(html):
        pm = PRICE_RE.search(html, m.end(), m.end() + LOOKAHEAD)
        if pm:
            value = sane(pm.group(1))
            if value is not None:
                return value
    # No handle match: fall back to the JSON-LD offer, sentinel filtered out.
    for m in LD_PRICE_RE.finditer(html):
        value = sane(m.group(1))
        if value is not None:
            return value
    return None


async def unpriced(brand: str) -> List[Dict[str, str]]:
    return await db.fetch(
        "select sku, name, url from products "
        "where brand = %s and price is null and url is not null order by name",
        (brand,),
    )


async def set_price(sku: str, price: float) -> None:
    await db.execute("update products set price = %s, updated_at = now() where sku = %s",
                     (price, sku))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brand", default="soundcore")
    parser.add_argument("--skip-products", action="store_true",
                        help="collections pass only — no per-product requests")
    parser.add_argument("--limit", type=int, default=400)
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO)

    await db.init_pool()
    try:
        rows = await unpriced(args.brand)
        started = len(rows)
        log.info("backfill.start", brand=args.brand, unpriced=started)
        if not rows:
            print(f"{args.brand}: nothing to backfill")
            return 0

        fixed = 0
        async with Crawler() as crawler:
            # Pass 1 — collections.
            index: Dict[str, float] = {}
            for url in COLLECTIONS.get(args.brand, []):
                html = await crawler.get(url)
                if not html:
                    continue
                found = price_index(html)
                new = {k: v for k, v in found.items() if k not in index}
                index.update(new)
                log.info("backfill.collection", url=url.split("/")[-1],
                         priced=len(found), new=len(new), total=len(index))

            remaining: List[Dict[str, str]] = []
            for row in rows:
                value = index.get(handle_of(row["url"]))
                if value is None:
                    remaining.append(row)
                    continue
                await set_price(row["sku"], value)
                fixed += 1
            log.info("backfill.pass1", fixed=fixed, remaining=len(remaining))

            # Pass 2 — one request per product still missing a price.
            if not args.skip_products:
                for row in remaining[: args.limit]:
                    html = await crawler.get(row["url"])
                    if not html:
                        continue
                    value = price_for_handle(html, handle_of(row["url"]))
                    if value is None:
                        continue
                    await set_price(row["sku"], value)
                    fixed += 1

        left = len(await unpriced(args.brand))
        print(f"\n{args.brand}: priced {fixed} of {started}; {left} still unpriced "
              f"(likely genuinely unavailable)")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
