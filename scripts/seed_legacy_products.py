"""Register products that are discontinued but still in customers' homes.

The crawler can only see what the storefront still sells. The eufy Robot Vacuum Omni
S1 Pro is a real example of the gap: its product page now 301-redirects to a
"discontinued, here are the alternatives" blog post, so a sitemap crawl brings back its
spare parts and nothing else.

That matters twice over:

  - Support does not stop when sales do. Someone bought one last year, it is still under
    warranty, and "I can't find that product" is the worst possible answer.
  - Scenario S2 depends on it. "S1 Pro" is ambiguous precisely because it names both a
    robot vacuum and a wearable breast pump. Lose the vacuum and the ambiguity — the
    thing the demo is built to show — silently disappears.

Only facts that survive checking go in here: name, brand, category, status, and the
official page that documents the discontinuation. No invented specs or prices — the
guard rules would rightly refuse to let the agent cite them anyway.

    python scripts/seed_legacy_products.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from typing import Any, Dict, List

import structlog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402

log = structlog.get_logger("legacy")

LEGACY: List[Dict[str, Any]] = [
    {
        "brand": "eufy", "sku": "T2080111", "name": "eufy Robot Vacuum Omni S1 Pro",
        "category": "robot_vacuum", "form_factor": "robot_vacuum",
        "status": "discontinued", "warranty_months": 12,
        "url": "https://www.eufy.com/blogs/robovac/eufy-s1-pro-discontinued",
        "note": "Discontinued; the product page redirects to eufy's discontinuation "
                "notice. Still supported and still under warranty for recent buyers.",
    },
    {
        "brand": "eufy", "sku": "T2080V11", "name": "eufy Robot Vacuum Omni S1",
        "category": "robot_vacuum", "form_factor": "robot_vacuum",
        "status": "discontinued", "warranty_months": 12,
        "url": "https://www.eufy.com/blogs/robovac/eufy-s1-pro-discontinued",
        "note": "Discontinued alongside the S1 Pro.",
    },
]


async def main() -> int:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(processors=[structlog.processors.add_log_level,
                                    structlog.dev.ConsoleRenderer(colors=False)])

    await db.init_pool()
    try:
        for p in LEGACY:
            row = await db.fetch_one(
                """
                insert into products (brand, sku, name, category, form_factor, url, status,
                                      warranty_months, currency, raw)
                values (%s,%s,%s,%s,%s,%s,%s,%s,'USD',%s)
                on conflict (sku) do update set
                    name = excluded.name, category = excluded.category,
                    form_factor = excluded.form_factor, status = excluded.status,
                    url = excluded.url, warranty_months = excluded.warranty_months,
                    raw = excluded.raw, updated_at = now()
                returning id::text as id
                """,
                (p["brand"], p["sku"], p["name"], p["category"], p["form_factor"],
                 p["url"], p["status"], p["warranty_months"],
                 json.dumps({"description": p["note"], "source": "legacy_catalog"})),
            )
            log.info("legacy.upserted", sku=p["sku"], name=p["name"])

            # A discontinued product still needs an alias, and the derived-alias rebuild
            # would not produce one from a name the crawler never saw.
            await db.execute(
                """
                insert into product_aliases (alias, product_id, confidence, source)
                values (%s, %s, 1.0, 'manual')
                on conflict (alias, product_id) do nothing
                """,
                ("s1 pro" if "S1 Pro" in p["name"] else "omni s1", row["id"]),
            )

        rows = await db.fetch(
            """
            select p.sku, p.name, p.category, p.status
            from product_aliases a join products p on p.id = a.product_id
            where a.alias = %s order by p.category
            """,
            ("s1 pro",),
        )
        print("\n's1 pro' now resolves to:")
        for r in rows:
            print(f"  {r['sku']:14} {(r['category'] or '?'):14} {r['status']:14} {r['name'][:55]}")
        categories = {r["category"] for r in rows if r["category"]}
        ok = len(rows) >= 2 and len(categories) >= 2
        print(f"\n{'PASS' if ok else 'FAIL'}: {len(rows)} products across "
              f"{len(categories)} categories {sorted(categories)}")
        return 0 if ok else 1
    finally:
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
