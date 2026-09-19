"""Compare every catalogue row against its own product page on the live site.

For each product with a `url`, fetch the page and read what the store says today: the
page title, the price, the SKUs on the page, and whether the page exists at all. Report
the rows where the DB disagrees. Read-only: it writes a report, never the DB.

    python scripts/audit_catalogue_vs_site.py                 # all rows with a url
    python scripts/audit_catalogue_vs_site.py --category robot_vacuum
    python scripts/audit_catalogue_vs_site.py --limit 50

Report: logs/catalogue_vs_site.json (+ a summary on stdout).
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import pathlib
import re
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.clients import db  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

# A price differs when it is off by more than a cent and more than 1% (regional rounding).
PRICE_TOLERANCE = 0.01


def read_page(body: str) -> dict:
    title = re.search(r"<title>([^<]*)", body)
    prices = re.findall(r'"price"\s*:\s*"?(\d+(?:\.\d+)?)"?', body)
    skus = set(s.upper() for s in re.findall(r'"sku"\s*:\s*"([^"]{3,60})"', body))
    return {
        "title": html.unescape(title.group(1)).strip() if title else "",
        "price": float(prices[0]) if prices else None,
        "skus": sorted(skus),
    }


async def check(client: httpx.AsyncClient, row: dict, sem: asyncio.Semaphore) -> dict:
    out = {"sku": row["sku"], "name": row["name"], "category": row["category"],
           "db_price": float(row["price"]) if row["price"] is not None else None,
           "url": row["url"], "issues": []}
    async with sem:
        try:
            r = await client.get(row["url"])
        except Exception as e:  # noqa: BLE001
            out["issues"].append(f"fetch_failed: {type(e).__name__}")
            return out
    out["status"] = r.status_code
    if r.status_code == 404:
        out["issues"].append("page_gone")
        return out
    if r.status_code >= 400:
        out["issues"].append(f"http_{r.status_code}")
        return out
    page = read_page(r.text)
    out.update(site_title=page["title"], site_price=page["price"])
    if "/products/" in row["url"] and "/products/" not in str(r.url):
        out["issues"].append(f"redirected_off_product: {r.url}")
    if page["price"] is not None and out["db_price"] is not None:
        diff = abs(page["price"] - out["db_price"])
        if diff > PRICE_TOLERANCE and diff > 0.01 * page["price"]:
            out["issues"].append(f"price: db {out['db_price']} vs site {page['price']}")
    if out["db_price"] is None and page["price"]:
        out["issues"].append(f"price missing in db (site {page['price']})")
    base = row["sku"].upper()
    if page["skus"] and not base.startswith(("BUNDLE", "COMBO", "GID:")) \
            and not any(s == base or s.startswith(base) or base.startswith(s)
                        for s in page["skus"]):
        out["issues"].append(f"sku not on page (page has {page['skus'][:4]})")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out", default="logs/catalogue_vs_site.json")
    args = ap.parse_args()

    await db.init_pool()
    sql = ("select sku, name, category, price, url from products "
           "where status <> 'invalid' and url like 'http%%'")
    params: tuple = ()
    if args.category:
        sql += " and category = %s"
        params = (args.category,)
    sql += " order by category, sku"
    if args.limit:
        sql += f" limit {int(args.limit)}"
    rows = await db.fetch(sql, params)
    await db.close_pool()

    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(headers=UA, timeout=30, follow_redirects=True) as client:
        results = await asyncio.gather(*(check(client, r, sem) for r in rows))

    bad = [r for r in results if r["issues"]]
    kinds: dict = {}
    for r in bad:
        for i in r["issues"]:
            k = i.split(":")[0]
            kinds[k] = kinds.get(k, 0) + 1
    pathlib.Path(args.out).parent.mkdir(exist_ok=True)
    pathlib.Path(args.out).write_text(json.dumps(results, indent=1, ensure_ascii=False),
                                      encoding="utf-8")
    print(f"checked {len(results)} rows · {len(bad)} disagree with the site")
    for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {k}: {n}")
    print(f"report: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
