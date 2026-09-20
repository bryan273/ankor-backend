"""Fetch the missing product photos from the product pages themselves.

599 of 1,491 rows have no `hero_image`, and a card with no photograph is a grey box with
the brand name in it. The store's own page carries the picture in its Open Graph tag —
the same image the customer sees — so this reads `og:image` (falling back to
`twitter:image` and to JSON-LD `"image"`), and writes it to the rows that have none.

Read-only by default, like the catalogue audit next to it.

    python scripts/backfill_hero_images.py              # dry run, writes a report
    python scripts/backfill_hero_images.py --apply
    python scripts/backfill_hero_images.py --restore logs/hero_backfill_<time>.json
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
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

# A logo or a share banner is not a product photo, and one of those on every card is
# worse than the honest grey box it replaces.
REJECT = re.compile(r"(logo|favicon|placeholder|share[-_]?image|og[-_]?default|banner)", re.I)


def find_image(body: str) -> str:
    for pattern in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
        r'"image"\s*:\s*"([^"]+)"',
        r'"image"\s*:\s*\[\s*"([^"]+)"',
    ):
        m = re.search(pattern, body)
        if not m:
            continue
        url = html.unescape(m.group(1)).strip()
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http") and not REJECT.search(url):
            return url
    return ""


async def one(client: httpx.AsyncClient, row: dict, sem: asyncio.Semaphore) -> dict:
    out = {"sku": row["sku"], "name": row["name"], "url": row["url"], "image": "",
           "note": ""}
    async with sem:
        try:
            r = await client.get(row["url"])
        except Exception as e:  # noqa: BLE001
            out["note"] = f"fetch_failed: {type(e).__name__}"
            return out
    if r.status_code >= 400:
        out["note"] = f"http_{r.status_code}"
        return out
    out["image"] = find_image(r.text)
    if not out["image"]:
        out["note"] = "no_image_on_page"
    return out


@contextlib.asynccontextmanager
async def _transaction():
    async with db.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                yield cur


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    await db.init_pool()
    try:
        if args.restore:
            snap = json.loads(pathlib.Path(args.restore).read_text(encoding="utf-8"))
            async with _transaction() as cur:
                for r in snap:
                    await cur.execute("update products set hero_image = %s where sku = %s",
                                      (r["was"], r["sku"]))
            print(f"restored {len(snap)} rows")
            return

        sql = ("select sku, name, url from products where status <> 'invalid' "
               "and hero_image is null and url like 'http%%' order by sku")
        if args.limit:
            sql += f" limit {int(args.limit)}"
        rows = await db.fetch(sql)
        print(f"{len(rows)} rows without a photo")

        sem = asyncio.Semaphore(args.concurrency)
        async with httpx.AsyncClient(headers=UA, timeout=30, follow_redirects=True) as c:
            found = await asyncio.gather(*(one(c, r, sem) for r in rows))

        good = [f for f in found if f["image"]]
        pathlib.Path("logs").mkdir(exist_ok=True)
        pathlib.Path("logs/hero_backfill_report.json").write_text(
            json.dumps(found, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"found {len(good)} images · {len(found) - len(good)} without one")
        if not args.apply:
            for f in good[:10]:
                print("   ", f["sku"], f["image"][:90])
            print("dry run: nothing written. Re-run with --apply.")
            return

        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = pathlib.Path(f"logs/hero_backfill_{stamp}.json")
        path.write_text(json.dumps([{"sku": f["sku"], "was": None} for f in good],
                                   indent=1), encoding="utf-8")
        async with _transaction() as cur:
            for f in good:
                await cur.execute(
                    "update products set hero_image = %s, updated_at = now() "
                    "where sku = %s and hero_image is null", (f["image"], f["sku"]))
        print(f"applied {len(good)} · undo file {path}")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
