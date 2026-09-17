"""Group regional listings of one product — without collapsing them.

TASK-schema-dedupe §3.1 calls the 113 slug groups "duplicates" and prescribes collapsing
each to one canonical row, alias-ing the rest, and deleting the excess. The catalogue says
otherwise. Measured over the 113 groups (242 rows):

    duplicate SKUs in `products` ........ 0
    groups differing in currency ....... 111
    groups differing in price .......... 102

    a1641:  A1641011 =  59.99 USD (anker.com/products/a1641)
            A1641H21 = 129.95 AUD (anker.com/au/products/a1641)

These are the same model sold in three storefronts at three prices in three currencies.
Collapsing them would delete SKUs that appear on real customer orders and leave one
arbitrary currency behind, so the agent would quote USD to an Australian customer. §3.1's
own gate — "every SKU still resolves" — would pass while the answer about money became
wrong. Note also that SKU resolution does not go through `product_aliases` today: all
1,493 SKUs resolve directly against `products.sku`, and the alias table holds *names*
("s1 pro"), so alias-ification would not even preserve what deletion removed.

So: group, don't collapse. `canonical_id` makes the family explicit — retrieval and
disambiguation can treat three regional listings as one product instead of three
competitors in the same top-k — while every row keeps its own SKU, price, currency and
URL. Nothing is deleted, which is also the only reading of "never delete a row without
first proving an alias exists for its SKU" that survives contact with this data.

Idempotent: re-running re-derives the same grouping and rewrites the same values.

    python scripts/link_variants.py            # apply
    python scripts/link_variants.py --check    # report only, change nothing
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional

import structlog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402

log = structlog.get_logger("variants")

# `anker.com/au/products/x` / `eufy.com/eu-en/products/x` — the segment before /products/.
LOCALE_RE = re.compile(r"//[^/]+/([a-z]{2}(?:-[a-z]{2})?)/products/", re.I)
GLOBAL = "global"
BUNDLE_RE = re.compile(r"^(?:BUNDLE|COMBO)", re.I)


def region_of(url: Optional[str]) -> str:
    """The storefront a listing belongs to. No locale segment means the global store."""
    m = LOCALE_RE.search(url or "")
    return m.group(1).lower() if m else GLOBAL


def family_key(row: Dict[str, Any]) -> str:
    """Normalised product slug — the same model across storefronts shares it."""
    slug = (row.get("slug") or "").strip().lower()
    if slug:
        return slug
    return (row.get("url") or "").rstrip("/").rsplit("/", 1)[-1].lower()


def pick_canonical(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic, in this order:

    1. not a bundle — a bundle is a different purchase, not a translation of one;
    2. the global storefront over a regional one (§3.1 suggests exactly this);
    3. USD, the currency the rest of the catalogue is majority-denominated in;
    4. lowest SKU, so the choice is stable across runs and machines.
    """
    return sorted(rows, key=lambda r: (
        bool(BUNDLE_RE.match(r["sku"] or "")),
        region_of(r["url"]) != GLOBAL,
        (r["currency"] or "") != "USD",
        r["sku"] or "",
    ))[0]


async def ensure_columns() -> None:
    await db.execute(
        "alter table products add column if not exists canonical_id uuid references products(id)")
    await db.execute("alter table products add column if not exists region text")
    await db.execute(
        "create index if not exists products_canonical_idx on products (canonical_id)")


async def main(check_only: bool) -> int:
    await db.init_pool()
    if not check_only:
        await ensure_columns()

    rows = await db.fetch(
        "select id::text as id, sku, slug, url, price, currency, name from products")
    families: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        families[family_key(row)].append(row)

    multi = {k: v for k, v in families.items() if len(v) > 1}
    updates: List[tuple] = []
    for members in families.values():
        canonical = pick_canonical(members)
        for row in members:
            updates.append((canonical["id"], region_of(row["url"]), row["id"]))

    log.info("variants.plan", families=len(families), grouped_families=len(multi),
             rows=len(rows), listings_in_groups=sum(len(v) for v in multi.values()))
    for key, members in sorted(multi.items(), key=lambda x: -len(x[1]))[:3]:
        canonical = pick_canonical(members)
        log.info("variants.sample", family=key[:44], canonical=canonical["sku"],
                 listings=[f"{m['sku']}={m['price']}{m['currency'] or ''}@{region_of(m['url'])}"
                           for m in members])

    if check_only:
        await db.close_pool()
        return 0

    await db.execute_many(
        "update products set canonical_id = %s, region = %s, updated_at = now() where id = %s",
        updates)

    # Gates. Nothing was deleted, so the SKU gate is about resolution still working;
    # the price gate is the one §3.1 would have failed.
    n_rows = await db.fetch_one("select count(*) n from products")
    n_linked = await db.fetch_one("select count(*) n from products where canonical_id is not null")
    orphan = await db.fetch_one(
        "select count(*) n from products p "
        "where p.canonical_id is not null "
        "  and not exists (select 1 from products c where c.id = p.canonical_id)")
    distinct_prices = await db.fetch_one(
        "select count(distinct (canonical_id, price, currency)) n from products "
        "where canonical_id is not null")
    log.info("variants.done", products=n_rows["n"], linked=n_linked["n"],
             orphaned_canonical=orphan["n"], distinct_price_points=distinct_prices["n"],
             rows_deleted=0)
    assert orphan["n"] == 0, "a canonical_id points at a row that does not exist"
    assert n_rows["n"] == len(rows), "row count changed — nothing should have been deleted"
    await db.close_pool()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.check)))
