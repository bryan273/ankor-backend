"""Strip scraper markup out of `products.name`.

31 product names carry `<b>` tags around the model number — search-result highlighting
that the crawler copied verbatim:

    'Anker <b>323</b> Charger (33W)'
    'Anker <b>575</b> USB-C Docking Station (13-in-1)'
    'Anker <b>PowerExpand</b> M.2 SSD Enclosure'

That is ugly in a product card, and it would be easy to file as cosmetic. It is not.
Measured across the catalogue:

    HTML-named products:   31    average aliases 0.00   with ZERO aliases: 31 of 31
    clean-named products: 1462   average aliases 0.95   with ZERO aliases: 873

**Every single one lost its aliases.** `scripts/build_aliases.py` derives them from the
name, and the model number — the one token a customer actually types — is exactly what the
tags wrap. So "Anker 323 charger" could not reach `A2331321` through the alias path at
all, which is the deterministic half of disambiguation.

(The 873 clean-named products without aliases are a separate, milder matter: alias
coverage is sparse by design, derived only where a short name is inferable. 31 of 31 is
categorical, and that is the difference that made this worth chasing.)

After this runs, re-derive the aliases and re-embed, because the name is both the alias
source and part of the embedded product text:

    python scripts/clean_product_names.py
    python scripts/build_aliases.py
    python scripts/embed_corpus.py --only products

Idempotent: a name with no markup is left exactly as it is.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import pathlib
import re
import sys

import structlog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402

log = structlog.get_logger("names")

TAG = re.compile(r"<[^>]+>")


def clean(name: str) -> str:
    """Drop tags, decode entities, and collapse the whitespace they leave behind."""
    return re.sub(r"\s{2,}", " ", TAG.sub("", html.unescape(name or ""))).strip()


async def main(check_only: bool) -> int:
    await db.init_pool()
    rows = await db.fetch(
        "select id::text as id, sku, name from products where name ~ '<[^>]+>' order by sku")

    updates = []
    for row in rows:
        fixed = clean(row["name"])
        # A name that cleans to nothing is not a name — leave it and say so, rather than
        # writing an empty string into a NOT NULL column that the UI will render.
        if not fixed:
            log.warning("names.empty_after_clean", sku=row["sku"], name=row["name"][:60])
            continue
        updates.append((fixed, row["id"]))

    log.info("names.plan", with_markup=len(rows), to_update=len(updates))
    for row in rows[:5]:
        log.info("names.sample", sku=row["sku"], before=row["name"][:52],
                 after=clean(row["name"])[:52])

    if check_only or not updates:
        await db.close_pool()
        return 0

    await db.execute_many(
        "update products set name = %s, updated_at = now() where id = %s", updates)

    left = await db.fetch_one(
        "select count(*) as n from products where name ~ '<[^>]+>'")
    total = await db.fetch_one("select count(*) as n from products")
    log.info("names.done", updated=len(updates), still_with_markup=left["n"],
             products=total["n"], rows_deleted=0)
    log.info("names.next", run="scripts/build_aliases.py, then embed_corpus.py --only products")
    assert left["n"] == 0, "markup remains — the pattern and the cleaner disagree"
    await db.close_pool()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.check)))
