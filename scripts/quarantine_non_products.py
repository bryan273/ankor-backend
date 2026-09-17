"""Mark catalogue rows that are not products.

Two rows survived the crawl that are not things a customer can own:

    eufy-up-to-850-off                            "Up to $850 off"   (a promo banner)
    gid://shopify/ProductVariant/53422734901615   "100"              (a raw Shopify GID)

The first matters. It carries real alias rows — `850 off` and `up to 850 off` — so
`find_by_alias` can return it, and the agent would offer a customer a product called
"Up to $850 off". Its URL points at `t2070111`, a genuine product: the scraper read the
sale banner as the product name.

Rows are quarantined, never deleted. TASK-schema-dedupe's rule is "never delete a row
without first proving an alias exists for its SKU", and the safer reading of it is that
a scrape artifact is evidence about the crawler worth keeping. `status='invalid'` keeps
the row and the audit trail while taking it out of every retrieval path. The aliases
pointing AT it are deleted, because an alias for a non-product is pure noise.

Idempotent.

    python scripts/quarantine_non_products.py
    python scripts/quarantine_non_products.py --check
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

import structlog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402
from app.clients.vectors import NS_PRODUCTS, get_vectors  # noqa: E402

log = structlog.get_logger("quarantine")

# Deliberately an explicit list, not a pattern. A heuristic that guesses which rows are
# "not really products" will eventually quarantine a real one; each of these was read.
NOT_PRODUCTS = [
    "eufy-up-to-850-off",
    "gid://shopify/ProductVariant/53422734901615",
]


async def main(check_only: bool) -> int:
    await db.init_pool()
    rows = await db.fetch(
        "select sku, name, status from products where sku = any(%s)", (NOT_PRODUCTS,))
    aliases = await db.fetch(
        "select a.alias, p.sku from product_aliases a join products p on p.id = a.product_id "
        "where p.sku = any(%s)", (NOT_PRODUCTS,))
    for row in rows:
        log.info("quarantine.row", sku=row["sku"], name=row["name"], status=row["status"])
    for a in aliases:
        log.info("quarantine.alias_to_drop", alias=a["alias"], sku=a["sku"])

    if check_only:
        await db.close_pool()
        return 0

    dropped = await db.execute(
        "delete from product_aliases a using products p "
        "where p.id = a.product_id and p.sku = any(%s)", (NOT_PRODUCTS,))
    marked = await db.execute(
        "update products set status = 'invalid', updated_at = now() "
        "where sku = any(%s) and status is distinct from 'invalid'", (NOT_PRODUCTS,))

    # The row is out of every SQL path now, but its vector is not: `embed_products`
    # wrote `prod_<id>` into Pinecone on an earlier run, and a vector outlives the row
    # it came from. Left alone it keeps matching queries and keeps taking a top-k slot.
    ids = [f"prod_{r['id']}" for r in await db.fetch(
        "select id::text as id from products where sku = any(%s)", (NOT_PRODUCTS,))]
    vectors = get_vectors()
    try:
        vectors_deleted = await vectors.delete_ids(ids, NS_PRODUCTS)
    finally:
        await vectors.aclose()

    still = await db.fetch_one(
        "select count(*) n from products where sku = any(%s)", (NOT_PRODUCTS,))
    log.info("quarantine.done", aliases_deleted=dropped, rows_marked=marked,
             vectors_deleted=vectors_deleted,
             rows_still_present=still["n"], rows_deleted=0)
    assert still["n"] == len(NOT_PRODUCTS), "a product row was deleted — it must not be"
    await db.close_pool()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.check)))
