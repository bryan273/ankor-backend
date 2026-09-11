"""Crawl the Anker family product catalog into Supabase.

Sources are the public product sitemaps on each brand domain. We stay polite: one
request at a time per host with a delay, an identifying User-Agent, and an on-disk
cache so a re-run costs nothing and a crashed run resumes instead of restarting.

Extraction prefers JSON-LD `Product` markup, which these storefronts publish and which
is far more reliable than scraping a DOM that changes weekly. The DOM is the fallback.

The acceptance test for this script is not "how many products" — it is scenario S2:
`"s1 pro"` must resolve to at least two products in different categories. If eufy Baby
did not get crawled, that ambiguity does not exist and the headline demo silently
becomes a normal lookup.

    python scripts/crawl_products.py --limit 400
    python scripts/crawl_products.py --brands eufy --limit 100
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import re
import sys
import html as html_lib
import time
from typing import Any, Dict, Iterable, List, Optional, Set
from xml.etree import ElementTree

import httpx
import structlog
from selectolax.parser import HTMLParser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402  — event-loop policy on Windows
from app.clients import db  # noqa: E402
from app.config import settings  # noqa: E402

log = structlog.get_logger("crawl")

CACHE = pathlib.Path("data/cache")
DELAY = 0.7  # seconds between requests to one host

SITEMAPS: Dict[str, List[str]] = {
    "anker": ["https://www.anker.com/server-sitemap-index-products.xml",
              "https://www.anker.com/sitemap.xml"],
    "eufy": ["https://www.eufy.com/server-sitemap-index-products.xml",
             "https://www.eufy.com/sitemap.xml"],
    "soundcore": ["https://www.soundcore.com/server-sitemap-index-products.xml",
                  "https://www.soundcore.com/sitemap.xml"],
    "ankersolix": ["https://www.ankersolix.com/server-sitemap-index-products.xml"],
    "ankerwork": ["https://www.ankerwork.com/server-sitemap-index-products.xml"],
}

PRODUCT_URL_RE = re.compile(r"/products?/", re.IGNORECASE)
# A product URL that is really editorial. The DOM fallback accepts any page with an
# <h1>, so without this a "best alternatives" blog post becomes a catalog entry —
# and then an alias, and then a candidate in the disambiguation picker.
NOT_A_PRODUCT_RE = re.compile(
    r"/(blogs?|collections|pages|articles|news|guides?|support|help)/", re.IGNORECASE)

# Category inference from the product name. Deliberately ordered: "breast pump" must be
# tested before generic terms, or every eufy Baby product becomes a robot vacuum.
CATEGORY_RULES: List[tuple[str, List[str]]] = [
    ("breast_pump", ["breast pump", "wearable pump", "s1 pro breast", "eufy baby", "milk"]),
    ("baby_monitor", ["baby monitor", "smart sock"]),
    ("robot_vacuum", ["robot vacuum", "robovac", "omni", "robot cleaner", "l60", "x10",
                      "e25", "e28", "s1 pro robot"]),
    ("stick_vacuum", ["stick vacuum", "cordless vacuum", "h30", "h20", "s11"]),
    ("security_camera", ["camera", "doorbell", "homebase", "solocam", "indoor cam",
                         "floodlight", "eufycam"]),
    ("smart_lock", ["smart lock", "video lock", "door lock"]),
    ("power_station", ["power station", "solix", "solar generator", "f3800", "c1000"]),
    ("power_bank", ["power bank", "powercore", "magsafe battery", "portable charger"]),
    ("charger", ["charger", "gan", "usb-c", "wall plug", "charging station", "prime",
                 "nano", "powerport"]),
    ("cable", ["cable", "powerline", "usb cable"]),
    ("audio", ["earbuds", "headphones", "liberty", "space", "soundcore", "speaker",
               "motion", "sleep a", "aerofit"]),
    ("projector", ["projector", "nebula", "capsule", "mars"]),
    ("webcam", ["webcam", "powerconf", "conference"]),
    ("printer", ["printer", "eufymake"]),
    ("mower", ["mower", "lawn"]),
]

BRAND_LABEL = {"anker": "Anker", "eufy": "eufy", "soundcore": "soundcore",
               "ankersolix": "Anker SOLIX", "ankerwork": "AnkerWork"}

WARRANTY_MONTHS = {"robot_vacuum": 12, "stick_vacuum": 12, "breast_pump": 12,
                   "baby_monitor": 12, "security_camera": 12, "smart_lock": 12,
                   "power_station": 60, "power_bank": 18, "charger": 18, "cable": 18,
                   "audio": 18, "projector": 12, "webcam": 24, "printer": 12, "mower": 24}


def clean_name(raw: str) -> str:
    """Unescape entities and strip the highlight markup storefronts leave in JSON-LD."""
    text = html_lib.unescape(raw or "")
    text = re.sub(r"</?[a-zA-Z][^>]{0,40}>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def infer_category(name: str, url: str = "") -> Optional[str]:
    blob = f"{name} {url}".lower()
    for category, needles in CATEGORY_RULES:
        if any(n in blob for n in needles):
            return category
    return None


def cache_path(url: str) -> pathlib.Path:
    return CACHE / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.html"


class Crawler:
    def __init__(self, delay: float = DELAY):
        self.delay = delay
        self.client: Optional[httpx.AsyncClient] = None
        self._last_hit: Dict[str, float] = {}
        self.fetched = 0
        self.from_cache = 0

    async def __aenter__(self) -> "Crawler":
        CACHE.mkdir(parents=True, exist_ok=True)
        self.client = httpx.AsyncClient(
            timeout=45.0, follow_redirects=True,
            headers={"User-Agent": settings.crawl_user_agent,
                     "Accept": "text/html,application/xhtml+xml,application/xml"},
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.client:
            await self.client.aclose()

    async def get(self, url: str, use_cache: bool = True) -> Optional[str]:
        path = cache_path(url)
        if use_cache and path.exists():
            self.from_cache += 1
            return path.read_text(encoding="utf-8", errors="ignore")

        host = httpx.URL(url).host or ""
        elapsed = time.monotonic() - self._last_hit.get(host, 0)
        if elapsed < self.delay:
            await asyncio.sleep(self.delay - elapsed)
        self._last_hit[host] = time.monotonic()

        try:
            r = await self.client.get(url)
            if r.status_code >= 400:
                log.debug("crawl.http_error", url=url[:90], status=r.status_code)
                return None
            self.fetched += 1
            path.write_text(r.text, encoding="utf-8")
            return r.text
        except httpx.HTTPError as e:
            log.debug("crawl.fetch_failed", url=url[:90], error=str(e)[:100])
            return None


async def sitemap_urls(crawler: Crawler, sitemap: str, depth: int = 0) -> List[str]:
    """Sitemaps nest: an index points at sitemaps that point at pages. Recurse once."""
    xml = await crawler.get(sitemap)
    if not xml:
        return []
    try:
        root = ElementTree.fromstring(xml.encode("utf-8", errors="ignore"))
    except ElementTree.ParseError:
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = [loc.text.strip() for loc in root.findall(".//sm:url/sm:loc", ns) if loc.text]
    nested = [loc.text.strip() for loc in root.findall(".//sm:sitemap/sm:loc", ns) if loc.text]
    if nested and depth < 2:
        interesting = [n for n in nested if "product" in n.lower()] or nested[:6]
        for child in interesting:
            urls.extend(await sitemap_urls(crawler, child, depth + 1))
    return urls


def json_ld_products(html: str) -> List[Dict[str, Any]]:
    """Pull every `Product` node out of the page's JSON-LD, including inside @graph."""
    tree = HTMLParser(html)
    found: List[Dict[str, Any]] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in _walk_ld(data):
            if str(item.get("@type", "")).lower() in ("product", "productgroup"):
                found.append(item)
    return found


def _walk_ld(data: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(data, dict):
        yield data
        for key in ("@graph", "itemListElement", "hasVariant", "isVariantOf"):
            for child in _walk_ld(data.get(key)):
                yield child
    elif isinstance(data, list):
        for entry in data:
            for child in _walk_ld(entry):
                yield child


def first_price(node: Any) -> Optional[float]:
    """Cheapest sane price anywhere under this node.

    Two shapes in the wild: a `Product` with `offers`, and a `ProductGroup` whose prices
    live inside `hasVariant[].offers`. Reading only the former left 207 soundcore
    products priceless. `_walk_ld` already descends into `hasVariant`, so walking the
    whole node rather than just its `offers` covers both.

    Out-of-stock variants are published with a 9999999.99 sentinel, which would sort to
    the top of a catalog as the most expensive thing Anker sells. Anything absurd is
    discarded, and the cheapest real variant wins — that is the "from" price a shopper
    expects to see.
    """
    prices: List[float] = []
    for entry in _walk_ld(node):
        for key in ("price", "lowPrice", "highPrice"):
            value = entry.get(key)
            if value in (None, ""):
                continue
            try:
                amount = float(str(value).replace(",", ""))
            except (ValueError, TypeError):
                continue
            if 0 < amount < 100_000:
                prices.append(amount)
    return min(prices) if prices else None


def first_image(node: Any) -> Optional[str]:
    image = node.get("image") if isinstance(node, dict) else None
    if isinstance(image, str):
        return image
    if isinstance(image, list) and image:
        return image[0] if isinstance(image[0], str) else image[0].get("url")
    if isinstance(image, dict):
        return image.get("url")
    return None


def og_image(html: str) -> Optional[str]:
    """The Open Graph image, which storefronts maintain for link previews even when
    their JSON-LD omits one."""
    node = HTMLParser(html).css_first('meta[property="og:image"]')
    return (node.attributes.get("content") or None) if node else None


def extract(html: str, url: str, brand: str) -> Optional[Dict[str, Any]]:
    """JSON-LD first, DOM as a fallback. Returns None when the page is not a product.

    The two paths are not exclusive for images. A page can publish a JSON-LD `Product`
    with no `image` field while still carrying a perfectly good `og:image` — soundcore
    does exactly that, and taking the JSON-LD branch wholesale left 207 products with no
    photo at all. So the structured data wins for facts, and the image falls back.
    """
    for node in json_ld_products(html):
        name = clean_name(node.get("name") or "")
        if not name or len(name) < 3:
            continue
        sku = (node.get("sku") or node.get("mpn") or node.get("productID") or "").strip()
        if not sku:
            sku = re.sub(r"[^a-z0-9]+", "-", f"{brand}-{name}".lower())[:60]
        category = infer_category(name, url)
        return {
            "brand": BRAND_LABEL.get(brand, brand), "sku": sku[:60], "name": name[:200],
            "slug": url.rstrip("/").split("/")[-1][:120],
            "category": category, "form_factor": category,
            "price": first_price(node),
            "currency": "USD", "url": url,
            "hero_image": ((first_image(node) or og_image(html) or "")[:500]
                           or None),
            "status": "active",
            "warranty_months": WARRANTY_MONTHS.get(category or "", 12),
            "description": (node.get("description") or "")[:1500],
            "specs": _specs_from_ld(node),
        }

    tree = HTMLParser(html)
    # A discontinued product 301s to its "here are the alternatives" blog post, and
    # because we follow redirects the HTML we get back is editorial while the URL we
    # asked for still looks like a product. The canonical link is what tells us where we
    # actually landed, so check it before the DOM fallback accepts any page with an <h1>.
    canonical = tree.css_first('link[rel="canonical"]') or tree.css_first('meta[property="og:url"]')
    landed = (canonical.attributes.get("href") or canonical.attributes.get("content") or ""
              if canonical else "")
    if landed and NOT_A_PRODUCT_RE.search(landed):
        return None

    title_node = tree.css_first("h1") or tree.css_first("title")
    if not title_node:
        return None
    name = clean_name(re.sub(r"\s*\|\s*(Anker|eufy|soundcore).*$", "",
                             title_node.text(strip=True)))[:200]
    if len(name) < 3:
        return None
    category = infer_category(name, url)
    og = tree.css_first('meta[property="og:image"]')
    desc = tree.css_first('meta[name="description"]')
    return {
        "brand": BRAND_LABEL.get(brand, brand),
        "sku": re.sub(r"[^a-z0-9]+", "-", f"{brand}-{name}".lower())[:60],
        "name": name, "slug": url.rstrip("/").split("/")[-1][:120],
        "category": category, "form_factor": category, "price": None, "currency": "USD",
        "url": url, "hero_image": (og.attributes.get("content") if og else None),
        "status": "active", "warranty_months": WARRANTY_MONTHS.get(category or "", 12),
        "description": (desc.attributes.get("content") if desc else "") or "",
        "specs": {},
    }


def _specs_from_ld(node: Dict[str, Any]) -> Dict[str, str]:
    specs: Dict[str, str] = {}
    props = node.get("additionalProperty")
    for prop in _walk_ld(props):
        key, value = prop.get("name"), prop.get("value")
        if key and value not in (None, ""):
            specs[str(key)[:60]] = str(value)[:200]
    for key in ("color", "material", "weight", "model"):
        if node.get(key):
            specs[key] = str(node[key])[:200]
    return specs


async def save(product: Dict[str, Any]) -> Optional[str]:
    row = await db.fetch_one(
        """
        insert into products (brand, sku, name, slug, category, form_factor, price, currency,
                              url, hero_image, status, warranty_months, raw)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (sku) do update set
            name = excluded.name, price = coalesce(excluded.price, products.price),
            url = excluded.url, hero_image = coalesce(excluded.hero_image, products.hero_image),
            category = coalesce(excluded.category, products.category),
            updated_at = now()
        returning id::text as id
        """,
        (product["brand"], product["sku"], product["name"], product.get("slug"),
         product.get("category"), product.get("form_factor"), product.get("price"),
         product.get("currency", "USD"), product.get("url"), product.get("hero_image"),
         product.get("status", "active"), product.get("warranty_months"),
         json.dumps({"description": product.get("description", "")})),
    )
    product_id = row["id"]
    specs = product.get("specs") or {}
    if specs:
        await db.execute_many(
            """
            insert into product_specs (product_id, key, value) values (%s,%s,%s)
            on conflict (product_id, key) do update set value = excluded.value
            """,
            [(product_id, k, v) for k, v in list(specs.items())[:40]],
        )
    if product.get("hero_image"):
        await db.execute(
            """
            insert into product_media (product_id, url, kind, ord) values (%s,%s,'image',0)
            on conflict (product_id, url) do nothing
            """,
            (product_id, product["hero_image"]),
        )
    return product_id


async def crawl_brand(crawler: Crawler, brand: str, limit: int, seen: Set[str],
                      seeds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    urls: List[str] = []
    for sitemap in SITEMAPS.get(brand, []):
        urls = await sitemap_urls(crawler, sitemap)
        if urls:
            log.info("crawl.sitemap", brand=brand, sitemap=sitemap.split("/")[-1], urls=len(urls))
            break
    product_urls = [u for u in urls
                    if PRODUCT_URL_RE.search(u) and not NOT_A_PRODUCT_RE.search(u)
                    and u not in seen]
    # Deduplicate on the slug: storefronts publish the same product under several paths.
    unique: Dict[str, str] = {}
    for u in product_urls:
        unique.setdefault(u.rstrip("/").split("/")[-1].lower(), u)
    product_urls = list(unique.values())[:limit]
    if seeds:
        # Seeds jump the queue. Sampling a 6,600-URL sitemap can easily miss an entire
        # sub-brand, and losing eufy Baby would quietly delete scenario S2 — the "S1 Pro"
        # ambiguity only exists because a breast pump and a robot vacuum share the name.
        for s in reversed([u for u in seeds if brand in u]):
            if s not in product_urls:
                product_urls.insert(0, s)
    log.info("crawl.brand_start", brand=brand, candidates=len(product_urls))

    saved: List[Dict[str, Any]] = []
    for i, url in enumerate(product_urls, 1):
        seen.add(url)
        html = await crawler.get(url)
        if not html:
            continue
        product = extract(html, url, brand)
        if not product:
            continue
        try:
            product["product_id"] = await save(product)
            saved.append(product)
        except Exception as e:  # noqa: BLE001 — one bad page must not end the crawl
            log.warning("crawl.save_failed", url=url[:80], error=str(e)[:120])
        if i % 25 == 0:
            log.info("crawl.progress", brand=brand, done=i, saved=len(saved))
    log.info("crawl.brand_done", brand=brand, saved=len(saved))
    return saved


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brands", default="anker,eufy,soundcore,ankersolix,ankerwork")
    parser.add_argument("--limit", type=int, default=200, help="max product URLs per brand")
    parser.add_argument("--delay", type=float, default=DELAY)
    parser.add_argument("--seeds", default="", help="comma-separated product URLs to crawl first")
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(processors=[structlog.processors.add_log_level,
                                    structlog.dev.ConsoleRenderer(colors=False)])

    await db.init_pool()
    seeds = [u.strip() for u in args.seeds.split(',') if u.strip()]
    total: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    async with Crawler(args.delay) as crawler:
        for brand in [b.strip() for b in args.brands.split(",") if b.strip()]:
            try:
                total.extend(await crawl_brand(crawler, brand, args.limit, seen, seeds))
            except Exception as e:  # noqa: BLE001
                log.error("crawl.brand_failed", brand=brand, error=str(e)[:200])

    by_category: Dict[str, int] = {}
    for p in total:
        by_category[p.get("category") or "uncategorised"] = \
            by_category.get(p.get("category") or "uncategorised", 0) + 1
    log.info("crawl.done", saved=len(total), fetched=crawler.fetched,
             cached=crawler.from_cache)
    for category, n in sorted(by_category.items(), key=lambda kv: -kv[1]):
        print(f"  {category:20} {n}")

    row = await db.fetch_one("select count(*) as n from products")
    print(f"\nproducts in database: {row['n']}")
    await db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
