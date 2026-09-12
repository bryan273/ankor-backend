"""Give the uncategorised products a category.

The crawl infers a category from the product name, and roughly a tenth of the catalog
comes back with none — spare parts, replacement filters, carry cases, gift cards. That
is invisible in a chat transcript and glaring in a storefront: a category rail that
covers 89% of the catalog quietly hides the dustbin the customer is looking for.

Only rows with NO category are touched. A product the crawl already classified keeps
its classification, so running this can never reshuffle the catalog — the worst case is
that it changes nothing.

    python scripts/backfill_categories.py            # apply
    python scripts/backfill_categories.py --dry-run  # show what would change
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402  — event-loop policy on Windows
from app.clients import db  # noqa: E402
from scripts.crawl_products import WARRANTY_MONTHS, infer_category  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--recompute", action="store_true",
        help="re-run the rules over products that ALREADY have a category, and move the "
             "ones the rules now classify differently. Use after changing a rule — "
             "always with --dry-run first, since this one can move the whole catalog.",
    )
    args = parser.parse_args()

    await db.init_pool()
    try:
        rows = await db.fetch(
            "select sku, name, url, category from products"
            + ("" if args.recompute else " where category is null")
            + " order by name"
        )
        print(f"{len(rows)} products considered\n")

        updates, counts, unmatched, moves = [], collections.Counter(), [], []
        for row in rows:
            category = infer_category(row["name"], row["url"] or "")
            if not category:
                unmatched.append(row["name"])
                continue
            if category == row["category"]:
                continue
            if row["category"]:
                moves.append((row["category"], category, row["name"]))
            counts[category] += 1
            updates.append((category, WARRANTY_MONTHS.get(category), row["sku"]))

        for category, n in counts.most_common():
            print(f"  {category:<14} {n}")
        print(f"  {'(still none)':<14} {len(unmatched)}")

        if unmatched[:12]:
            print("\nstill uncategorised, first few:")
            for name in unmatched[:12]:
                print(f"  · {name[:70]}")

        if moves:
            print(f"\n{len(moves)} products would MOVE between categories:")
            by_pair = collections.Counter((old, new) for old, new, _ in moves)
            for (old, new), n in by_pair.most_common():
                print(f"  {old} -> {new}: {n}")
                for _, _, name in [m for m in moves if m[0] == old and m[1] == new][:3]:
                    print(f"      · {name[:64]}")

        if args.dry_run:
            print("\ndry run — nothing written")
            return 0

        if updates:
            await db.execute_many(
                "update products set category = %s, "
                "warranty_months = coalesce(warranty_months, %s), updated_at = now() "
                "where sku = %s",
                updates,
            )
        print(f"\nwrote {len(updates)} categories")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
