"""Crawl the official support knowledge base into `kb_articles`.

Source: the Salesforce-backed support communities at `support.eufy.com` and
`support.anker.com`, whose `robots.txt` allows crawling and which publish a sitemap of
every help article. Pages redirect to a server-rendered mirror, so the text is in the
HTML — no browser needed.

Two decisions worth knowing:

**Language.** The sitemap mixes every locale under the same slug space, and the same
article appears a dozen times in a dozen languages. Embedding all of them would bloat
the index and let a German passage answer an English question. We keep the English
ones; the agent replies in the customer's language regardless, because generation is
where translation belongs, not retrieval.

**Product linking.** Article titles end in a SKU token — "…User-Guide-E8P10",
"…Omni-E35-T210M". That token links the article to the product, which is what makes
`sku`-filtered retrieval possible. Without it, every chunk is unfiltered and a robot
vacuum answer can leak into a breast pump thread.

    python scripts/crawl_support.py --limit 400
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Set
from urllib.parse import unquote
from xml.etree import ElementTree

import structlog
from selectolax.parser import HTMLParser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402
from scripts.crawl_products import Crawler  # noqa: E402

log = structlog.get_logger("support")

SITEMAP_INDEXES = [
    "https://support.eufy.com/s/sitemap.xml",
    "https://support.anker.com/s/sitemap.xml",
]

# Boilerplate that wraps every article on this platform. Left in, it dominates the
# embedding: every chunk would look like a navigation menu, and every query would match
# every article equally well.
CHROME_PATTERNS = [
    r"Up to \$?\d+ Off.*?FanFest", r"Intelligence That Knows You",
    r"Eufy Search Log In", r"Anker Search Log In",
    r"Home Convenient Services Service Inquiries Parts & Accessories Contact Us Confirm",
    r"Was this article helpful.*$", r"Related Articles.*$", r"Contact Us.*Follow Us.*$",
    r"Copyright ©.*$", r"Privacy Policy.*Terms of Use", r"Sign up for.*newsletter",
    r"Cookie.*Settings", r"Choose your region",
]
CHROME_RE = re.compile("|".join(CHROME_PATTERNS), re.IGNORECASE | re.DOTALL)

# Non-English locale markers, either as a path segment or as script in the slug.
NON_LATIN_RE = re.compile(r"[　-鿿Ѐ-ӿ؀-ۿ가-힯]")
LOCALE_PATH_RE = re.compile(
    r"/(de|fr|es|it|ja|ko|zh|nl|pl|pt|ru|sv|fi|no|da|tr|ar|cs|hu|el|th|vi)/s/", re.IGNORECASE)
NON_ENGLISH_WORDS = re.compile(
    r"(Kayttoopas|Kaeyttoeopas|Anvandarhandledning|Benutzerhandbuch|Manuel|Manuale|"
    r"Handleiding|Bruksanvisning|Brugervejledning|Instrukcja|Kullanim|Uzivatelska|"
    r"Guia|Guide-d|Priruc)", re.IGNORECASE)

SKU_TOKEN_RE = re.compile(r"-([A-Z]\d{2,4}[A-Z0-9]{0,4})$", re.IGNORECASE)

DOC_TYPE_RULES = [
    ("manual", ["user-guide", "user-manual", "quick-start", "userguide", "manual"]),
    ("troubleshooting", ["troubleshoot", "not-working", "how-to-fix", "error", "issue",
                         "problem", "fails", "cannot", "won-t", "wont"]),
    ("faq", ["how-to", "how-do", "what-is", "can-i", "why-does", "where-is"]),
]


def doc_type_for(slug: str) -> str:
    lowered = slug.lower()
    for kind, needles in DOC_TYPE_RULES:
        if any(n in lowered for n in needles):
            return kind
    return "article"


def is_english(url: str) -> bool:
    decoded = unquote(url)
    if NON_LATIN_RE.search(decoded):
        return False
    if LOCALE_PATH_RE.search(decoded):
        return False
    if NON_ENGLISH_WORDS.search(decoded):
        return False
    return True


def title_from_slug(url: str) -> str:
    slug = unquote(url.rstrip("/").split("/")[-1])
    slug = re.sub(r"\?.*$", "", slug)
    slug = SKU_TOKEN_RE.sub("", slug)
    return re.sub(r"[-_]+", " ", slug).strip()


def sku_token(url: str) -> Optional[str]:
    slug = unquote(url.rstrip("/").split("/")[-1])
    match = SKU_TOKEN_RE.search(re.sub(r"\?.*$", "", slug))
    return match.group(1).upper() if match else None


def extract_body(html: str) -> str:
    """Take the tightest container that actually holds prose.

    "User Guide" articles are a title plus a link to a PDF — their `<main>` is about 90
    characters. Taking `<main>` unconditionally would store those as articles, and the
    index would fill with headings that answer nothing. So we walk from tightest to
    loosest and accept the first container with real text in it.
    """
    tree = HTMLParser(html)
    for node in tree.css("script, style, nav, header, footer, noscript, svg, form, iframe"):
        node.decompose()
    for selector in ("article", ".article-content", "[class*=articleBody]",
                     "[class*=articleDescription]", "main", "body"):
        node = tree.css_first(selector)
        if node is None:
            continue
        text = CHROME_RE.sub(" ", " ".join(node.text(separator=" ").split()))
        text = " ".join(text.split())
        if len(text) >= 400:
            return text
    return ""


def pdf_links(html: str) -> List[str]:
    """Manual articles wrap a PDF. The link is worth keeping even when the page text is
    not: it is a real citation target the agent can hand the customer."""
    tree = HTMLParser(html)
    out = []
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if ".pdf" in href.lower():
            out.append(href if href.startswith("http") else f"https:{href}"
                       if href.startswith("//") else href)
    return list(dict.fromkeys(out))[:4]


def priority(url: str) -> int:
    """Fetch order. Troubleshooting and FAQ articles carry the prose that answers a
    support question; manuals are mostly PDF stubs, so they go last."""
    kind = doc_type_for(url)
    return {"troubleshooting": 0, "faq": 1, "article": 2, "manual": 3}.get(kind, 2)


async def sitemap_children(crawler: Crawler, index_url: str) -> List[str]:
    xml = await crawler.get(index_url)
    if not xml:
        return []
    try:
        root = ElementTree.fromstring(xml.encode("utf-8", errors="ignore"))
    except ElementTree.ParseError:
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [loc.text.strip() for loc in root.findall(".//sm:sitemap/sm:loc", ns) if loc.text]


async def article_urls(crawler: Crawler, sitemap_url: str) -> List[str]:
    xml = await crawler.get(sitemap_url)
    if not xml:
        return []
    try:
        root = ElementTree.fromstring(xml.encode("utf-8", errors="ignore"))
    except ElementTree.ParseError:
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [loc.text.strip() for loc in root.findall(".//sm:url/sm:loc", ns) if loc.text]


def public_url(url: str, host: str) -> str:
    """Sitemaps list the internal Salesforce host; the public one redirects to a
    server-rendered mirror that actually returns text."""
    return re.sub(r"https://[^/]+/s/article/", f"https://{host}/s/article/", url)


async def link_products(sku: Optional[str], title: str) -> List[str]:
    """Return product ids this article is about. SKU token first, name match second."""
    if sku:
        rows = await db.fetch(
            "select id::text as id from products where upper(sku) like %s limit 5",
            (f"{sku}%",))
        if rows:
            return [r["id"] for r in rows]
    # Fall back to the most distinctive words of the title.
    words = [w for w in re.findall(r"[A-Za-z0-9]{4,}", title)
             if w.lower() not in {"guide", "user", "manual", "install", "setup", "start",
                                  "quick", "does", "with", "your", "from", "what", "when"}]
    if not words:
        return []
    rows = await db.fetch(
        "select id::text as id from products where name ilike %s limit 3",
        (f"%{' '.join(words[:2])}%",))
    return [r["id"] for r in rows]


async def save_doc(product_ids: List[str], title: str, url: str) -> None:
    """Manual PDFs, attached to whichever products the title identifies."""
    for pid in product_ids[:2] or [None]:
        await db.execute(
            """
            insert into product_docs (product_id, kind, title, url)
            values (%s, 'manual', %s, %s)
            on conflict (url) do nothing
            """,
            (pid, title[:200], url[:900]),
        )


async def save_article(url: str, title: str, doc_type: str, body: str,
                       product_ids: List[str]) -> bool:
    await db.execute(
        """
        insert into kb_articles (source_url, title, doc_type, product_ids, body, lang)
        values (%s,%s,%s,%s,%s,'en')
        on conflict (source_url) do update set
            title = excluded.title, body = excluded.body, doc_type = excluded.doc_type,
            product_ids = excluded.product_ids, fetched_at = now()
        """,
        (url, title[:300], doc_type, product_ids, body[:200000]),
    )
    return True


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=400, help="articles to fetch")
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(processors=[structlog.processors.add_log_level,
                                    structlog.dev.ConsoleRenderer(colors=False)])

    await db.init_pool()
    saved, skipped_lang, empty, docs_saved = 0, 0, 0, 0
    try:
        async with Crawler(args.delay) as crawler:
            candidates: List[tuple[str, str]] = []
            for index_url in SITEMAP_INDEXES:
                host = index_url.split("//")[1].split("/")[0]
                for child in await sitemap_children(crawler, index_url):
                    if "topicarticle" not in child and "article" not in child:
                        continue
                    urls = await article_urls(crawler, child)
                    log.info("support.sitemap", host=host, child=child.split("/")[-1],
                             urls=len(urls))
                    for u in urls:
                        candidates.append((u, host))

            english = [(u, h) for u, h in candidates if is_english(u)]
            skipped_lang = len(candidates) - len(english)
            log.info("support.filtered", total=len(candidates), english=len(english),
                     skipped_other_languages=skipped_lang)

            seen: Set[str] = set()
            unique: List[tuple[str, str]] = []
            for u, h in english:
                slug = u.rstrip("/").split("/")[-1].lower()
                if slug not in seen:
                    seen.add(slug)
                    unique.append((u, h))
            unique.sort(key=lambda pair: priority(pair[0]))

            for i, (url, host) in enumerate(unique[:args.limit], 1):
                fetch_url = public_url(url, host)
                html = await crawler.get(fetch_url)
                if not html:
                    continue
                title = title_from_slug(url)
                sku = sku_token(url)
                body = extract_body(html)
                if len(body) < 250:
                    # A manual stub still yields a citable PDF even with no prose.
                    for pdf in pdf_links(html):
                        product_ids = await link_products(sku, title)
                        await save_doc(product_ids, title, pdf)
                        docs_saved += 1
                    empty += 1
                    continue
                product_ids = await link_products(sku, title)
                await save_article(fetch_url, title, doc_type_for(url), body, product_ids)
                saved += 1
                if i % 25 == 0:
                    log.info("support.progress", done=i, saved=saved, empty=empty)

        stats = await db.fetch_one(
            """
            select count(*) as articles,
                   count(*) filter (where cardinality(product_ids) > 0) as linked,
                   avg(length(body))::int as avg_body
            from kb_articles
            """
        )
        print(f"\nkb_articles: {stats['articles']} "
              f"({stats['linked']} linked to a product), avg body {stats['avg_body']} chars")
        by_type = await db.fetch(
            "select doc_type, count(*) as n from kb_articles group by doc_type order by n desc")
        for r in by_type:
            print(f"  {r['doc_type']:18} {r['n']}")
        print(f"skipped (non-English): {skipped_lang}, no prose: {empty} (kept {docs_saved} manual PDFs)")
    finally:
        await db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
