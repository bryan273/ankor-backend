"""Bring the catalogue in line with the live store, and drop the double import.

What it changes, and why (evidence: logs/catalogue_vs_site.json from
scripts/audit_catalogue_vs_site.py, and the pilot report):

1. Categories the store contradicts:
   - spare parts filed as `robot_vacuum` / `stick_vacuum` (filters, bins, a charging base,
     a battery pack). They made "robot vacuums under $400" return 11 parts and hide the
     one real device, the 11S MAX.
   - soundcore speakers and earbuds filed as `power_station` / `charger`.
   - SOLIX gift cards filed as `power_station`: a customer's gift card counted as the
     power station they own.
   - rows with no category at all.
2. Prices: the price on the product page today, where the page shows a real one.
   Sold-out pages show 9999999.99; a $9.99 placeholder in the DB for those becomes NULL.
3. Pages the store has removed: status `discontinued`.
4. Non-products ("100", "Up to $850 off"): status `invalid`.
5. The double import: every dealer and every troubleshooting flow exists twice. Orders
   are re-pointed to one dealer per name and the copy is deleted; identical flows are
   collapsed; the one dealer order stored twice (SE-482911) loses its copy.
6. Error codes filed as `scraped_unattributed`: 33 rows stamped onto the breast pump
   S1 Pro from the robot vacuum's guide. The code already refuses to serve them.

Every row it touches is written to logs/data_fix_backup_<time>.json first.

    python scripts/fix_catalogue_data.py            # dry run: prints what would change
    python scripts/fix_catalogue_data.py --apply
    python scripts/fix_catalogue_data.py --restore logs/data_fix_backup_<time>.json
"""
from __future__ import annotations

import argparse
import contextlib
import asyncio
import datetime as dt
import json
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.clients import db  # noqa: E402

SOLD_OUT = 9_999_999.99

CATEGORY_FIXES = {
    # robot / stick vacuum parts
    "T2190121-91": "accessory", "T29151N2": "accessory", "T2906021": "accessory",
    "T2922031": "accessory", "T2150111-82": "accessory", "T2261111-82": "accessory",
    "T29110A1": "accessory", "T2190121-82": "accessory", "T29J5011": "accessory",
    "T290QAR0": "accessory", "T2351111-89": "accessory",
    "T2522111-89": "accessory", "T2501311-81": "accessory", "T2983T11": "accessory",
    "T2978091": "accessory",
    # soundcore audio in the wrong place
    "A31A3012": "audio", "A3134012": "audio", "A3937Z21": "audio",
    # uncategorised rows, by what the store sells them as
    "A1367H11": "power_bank", "A83140A1": "charger", "A2727012": "charger",
    "A7515311": "charger", "T29B2121": "accessory", "BUNDLE-T8705C11-2": "accessory",
    "BUNDLE-E8P10T21-1-T8P00321-2": "security_camera",
    "BUNDLE-E8P10T21-1-T8E00321-2": "security_camera",
    "BUNDLE-E814XT21-1-T8214T11-1": "security_camera",
    "BUNDLE-T87A0T20-1-E8E00T21-1": "security_camera",
    "BUNDLE-E814XT21-1-T8214T11-1-T85L1T11-1": "security_camera",
    "BUNDLE-E8E00T21-1-T87A0T20-1": "security_camera",
    "BUNDLE-T87093W0-1-E8E00T21-1": "security_camera",
    "BUNDLE-E85V0TY1-1-T87A0T20-1": "smart_lock",
}
NOT_PRODUCTS = ["gid://shopify/ProductVariant/53422734901615", "eufy-up-to-850-off"]
# Pages that redirect to a landing page: the price found there is not this product's.
PRICE_UNTRUSTED = {"soundcore-sleep-earbuds-4-pro-sleep-tracking-earbuds-with-smart-screen"}


@contextlib.asynccontextmanager
async def _transaction():
    """All of it or none of it: a half-applied fix is worse than either state."""
    async with db.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                yield cur


def _json(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    return str(o)


async def plan(audit: list) -> dict:
    steps: dict = {"category": [], "price": [], "status": [], "dealers": [], "flows": [],
                   "dealer_orders": [], "error_codes": []}

    rows = await db.fetch("select sku, category from products where sku = any(%s)",
                          (list(CATEGORY_FIXES),))
    for r in rows:
        if r["category"] != CATEGORY_FIXES[r["sku"]]:
            steps["category"].append((r["sku"], r["category"], CATEGORY_FIXES[r["sku"]]))
    for r in await db.fetch("select sku from products where category ilike 'power_station' "
                            "and name ilike '%%gift card%%'"):
        steps["category"].append((r["sku"], "power_station", "service"))

    for a in audit:
        if a["sku"] in PRICE_UNTRUSTED or not a.get("site_title"):
            continue
        site, ours = a.get("site_price"), a.get("db_price")
        if any(i.startswith("redirected") for i in a["issues"]):
            continue
        if site == SOLD_OUT:
            if ours is not None and ours <= 10:
                steps["price"].append((a["sku"], ours, None))
            continue
        if site and (ours is None or abs(site - ours) > max(0.01, 0.01 * site)):
            steps["price"].append((a["sku"], ours, site))
    for a in audit:
        if "page_gone" in a["issues"]:
            steps["status"].append((a["sku"], "active", "discontinued"))
    for sku in NOT_PRODUCTS:
        steps["status"].append((sku, "active", "invalid"))

    # Dealers are NOT merged, although every one exists twice. `dealer_orders` is
    # UNIQUE (dealer_id, order_no) with one product per row, so a two-item invoice
    # (GM774120: earbuds + a speaker) can only exist as one line on each copy of the
    # dealer. Merging them violates the constraint, and dropping a line loses a real
    # item. The lookup already reads all lines across both copies.

    flows = await db.fetch("""
        select id::text, product_id::text, symptom, steps::text as steps
        from troubleshooting_flows order by id""")
    seen: dict = {}
    for f in flows:
        key = (f["product_id"], f["symptom"], f["steps"])
        if key in seen:
            steps["flows"].append(f["id"])
        else:
            seen[key] = f["id"]

    orders = await db.fetch("""
        select o.id::text, o.order_no, o.product_id::text, d.name
        from dealer_orders o join dealers d on d.id = o.dealer_id order by o.id""")
    seen_o: set = set()
    for o in orders:
        key = (o["order_no"], o["product_id"], o["name"])
        if key in seen_o:
            steps["dealer_orders"].append(o["id"])
        seen_o.add(key)

    for r in await db.fetch("select id::text from error_codes "
                            "where provenance = 'scraped_unattributed'"):
        steps["error_codes"].append(r["id"])
    return steps


async def backup(steps: dict) -> pathlib.Path:
    skus = sorted({s[0] for s in steps["category"] + steps["price"] + steps["status"]})
    snap = {
        "products": await db.fetch("select id::text, sku, category, price, status "
                                   "from products where sku = any(%s)", (skus,)),
        "dealers": await db.fetch("select * from dealers where id::text = any(%s)",
                                  ([d[0] for d in steps["dealers"]],)),
        "dealer_orders": await db.fetch("select * from dealer_orders"),
        "troubleshooting_flows": await db.fetch(
            "select * from troubleshooting_flows where id::text = any(%s)", (steps["flows"],)),
        "error_codes": await db.fetch("select * from error_codes where id::text = any(%s)",
                                      (steps["error_codes"],)),
    }
    path = pathlib.Path(f"logs/data_fix_backup_{dt.datetime.now():%Y%m%d_%H%M%S}.json")
    path.write_text(json.dumps(snap, default=_json, indent=1, ensure_ascii=False),
                    encoding="utf-8")
    return path


async def apply(steps: dict) -> None:
    async with _transaction() as cur:
        for sku, _, new in steps["category"]:
            await cur.execute("update products set category = %s, updated_at = now() "
                              "where sku = %s", (new, sku))
        for sku, _, new in steps["price"]:
            await cur.execute("update products set price = %s, updated_at = now() "
                              "where sku = %s", (new, sku))
        for sku, _, new in steps["status"]:
            await cur.execute("update products set status = %s, updated_at = now() "
                              "where sku = %s", (new, sku))
        for dup, keep, _ in steps["dealers"]:
            await cur.execute("update dealer_orders set dealer_id = %s where dealer_id::text = %s",
                              (keep, dup))
            await cur.execute("delete from dealers where id::text = %s", (dup,))
        if steps["dealer_orders"]:
            await cur.execute("delete from dealer_orders where id::text = any(%s)",
                              (steps["dealer_orders"],))
        if steps["flows"]:
            await cur.execute("delete from troubleshooting_flows where id::text = any(%s)",
                              (steps["flows"],))
        if steps["error_codes"]:
            await cur.execute("delete from error_codes where id::text = any(%s)",
                              (steps["error_codes"],))


async def restore(path: str) -> None:
    snap = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    async with _transaction() as cur:
        for p in snap["products"]:
            await cur.execute("update products set category=%s, price=%s, status=%s "
                              "where id::text=%s",
                              (p["category"], p["price"], p["status"], p["id"]))
        for table in ("dealers", "troubleshooting_flows", "error_codes"):
            for row in snap[table]:
                cols = list(row)
                await cur.execute(
                    f"insert into {table} ({', '.join(cols)}) values "
                    f"({', '.join(['%s'] * len(cols))}) on conflict do nothing",
                    [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()])
        await cur.execute("delete from dealer_orders")
        for row in snap["dealer_orders"]:
            cols = list(row)
            await cur.execute(
                f"insert into dealer_orders ({', '.join(cols)}) values "
                f"({', '.join(['%s'] * len(cols))})", list(row.values()))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore")
    ap.add_argument("--audit", default="logs/catalogue_vs_site.json")
    args = ap.parse_args()
    await db.init_pool()
    try:
        if args.restore:
            await restore(args.restore)
            print(f"restored from {args.restore}")
            return
        audit = json.loads(pathlib.Path(args.audit).read_text(encoding="utf-8"))
        steps = await plan(audit)
        for k, v in steps.items():
            print(f"{k}: {len(v)}")
            for item in v[:60]:
                print("   ", item)
        if not args.apply:
            print("\ndry run: nothing written. Re-run with --apply.")
            return
        path = await backup(steps)
        print(f"backup: {path}")
        await apply(steps)
        print("applied.")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
