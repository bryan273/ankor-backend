"""Decide which articles deserve embedding quota, and clean the ones that do.

Written because the corpus and the quota pull in opposite directions. 12,080 articles sit
un-chunked; embedding them all is roughly 120k requests against a single free-tier key,
and the task doc records that a previous Google account was banned for exactly that kind
of traffic. So the question is not "can we embed it" but "which of it is worth a request".

The task doc measured body LENGTH and concluded every row carries usable text. Length is
not the same as content. Measured here instead:

- 12,080 un-chunked articles share **608 distinct titles**; 11,444 of them carry one of
  three site titles (`Anker`, `Soundcore`, `eufy Support | Troubleshooting …`).
- 59% of the bodies are the Salesforce "related articles" rail — link lists of the form
  `How should I wear Liberty 5 Pro? 548 views1 likes`.
- A large share is not English at all (`Керівництво користувача`, `Termini di utilizzo`).

None of that is a scrape defect. `crawl_support.py` already strips exactly this with
CHROME_PATTERNS and `is_english()`; the bulk import simply did not run through it. So this
applies the same rules the crawler applies, and then asks whether anything is left.

What survives cleaning is what gets embedded. Everything else is marked and left in place —
nothing is deleted, because the row is still evidence of what was scraped.

    python scripts/triage_kb_corpus.py                # measure only
    python scripts/triage_kb_corpus.py --apply        # write embed_status + cleaned body
    python scripts/triage_kb_corpus.py --show 5       # sample what each verdict looks like
"""
from __future__ import annotations

import argparse
import asyncio
import io
import collections
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
# Windows consoles default to cp1252; the corpus is full of non-Latin text and
# printing a sample must not kill the run.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import app  # noqa: F401,E402  — event-loop policy on Windows
from app.clients import db  # noqa: E402
from scripts.crawl_support import CHROME_PATTERNS  # noqa: E402

# The related-articles rail, which is most of what the bulk import captured. Each entry
# looks like "How do I reset Life A1? 269 views1 likes" — a link, not an answer.
#
# The character class deliberately allows "?" so the QUESTION is removed along with its
# view count. Excluding it left "What is MagSafe wireless charging?" behind: a real
# question with no answer under it, which is the worst thing to leave in a retrieval
# corpus — it matches a customer's phrasing perfectly and then says nothing.
# Greedy on purpose. Non-greedy matched only the digits immediately before "views" and
# left the question itself behind, which is the half that matters: "What is MagSafe
# wireless charging?" with no answer under it retrieves beautifully and answers nothing.
# Greedy walks back to the previous sentence end and takes the whole rail entry.
VIEWS_RAIL = re.compile(r"[^.!\n]{0,160}\d[\d,]*\s*views?\s*\d*\s*likes?", re.I)

# Site chrome the bulk import kept and the crawler strips.
EXTRA_CHROME = [
    r"Win a Mystery Box[^.]*?Claim Now",
    r"Anker Search Log In[^.]*?Confirm",
    r"Eufy Search Log In[^.]*?Confirm",
    r"Home Service Inquiries Manuals & Downloads Warranty Registration Contact Us",
    r"Go to Anker SOLIX",
    r"Anker Charging Anker SOLIX Anker eufy Anker eufyMake Anker soundcore Anker Innovations",
    r"🔥+[^.]{0,200}?>>",
    r"\b\d+\s+undefined\b",
]
CHROME_RE = [re.compile(p, re.I | re.S) for p in CHROME_PATTERNS + EXTRA_CHROME]

# Scripts we do not serve. The corpus is English-only by design — an article the agent
# cannot quote is quota spent on a passage no answer will ever cite.
NON_LATIN = re.compile(r"[Ѐ-ӿͰ-Ͽ֐-׿؀-ۿ"
                       r"　-鿿가-힯฀-๿]")
# Function words that are decisive for the Latin-script languages in this corpus.
NON_ENGLISH_HINTS = re.compile(
    r"\b(?:della|degli|utilizzo|条件|Nutzungsbedingungen|Datenschutz|condiciones|"
    r"utilisation|gebruiksvoorwaarden|villkor|betingelser|warunki|feltételek|"
    r"kullanım|termos|termini|användarhandbok|käyttöohje|Керівництво|инструкция)\b",
    re.I)

MIN_USEFUL_CHARS = 200

# Prose, or a list of links wearing prose's clothes?
#
# Stripping the view counts is not enough. What is left in a lot of these rows is a wall
# of *question titles* with no answers under them —
#
#   "What Should I Do If the Monitor Doesn't Work?  What Should I Do If the USB-C Port…"
#
# — plus file listings ("T8124_EU_Declaration_of_Conformity") and storefront nav
# ("Rave 3S | The Most Powerful AI Party Speaker: Buy Two, Enjoy $100 Off"). All three
# pass a length test and all three are worse than useless in a retrieval corpus: they
# match a customer's phrasing closely and then answer nothing.
#
# What separates them from real content is sentences. An answer explains something and
# ends sentences; a link list never does. Measured on hand-checked samples, real support
# text runs well above 5 sentence-endings per 1,000 characters and the link lists sit
# near zero.
SENTENCE_END = re.compile(r"[.!?][\s\"')\]]|[.!?]$")
MIN_SENTENCES_PER_1K = 5.0
# Storefront navigation reliably stacks short phrases separated by pipes.
PIPE_HEAVY = 6


def prose_density(text: str) -> float:
    """Sentence endings per 1,000 characters."""
    if not text:
        return 0.0
    return len(SENTENCE_END.findall(text)) / len(text) * 1000


def strip_chrome(body: str) -> str:
    text = body or ""
    for pattern in CHROME_RE:
        text = pattern.sub(" ", text)
    text = VIEWS_RAIL.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def looks_english(title: str, body: str) -> bool:
    sample = f"{title} {body[:800]}"
    if NON_LATIN.search(sample):
        return False
    return not NON_ENGLISH_HINTS.search(sample)


def triage(title: str, body: str) -> Tuple[str, str]:
    """Returns (verdict, cleaned_body).

    `keep` — real content survives cleaning, embed it.
    `chrome` — nothing but navigation once the rails are removed.
    `non_english` — we do not answer in this language, so it can never be cited.
    """
    cleaned = strip_chrome(body)
    if not looks_english(title or "", cleaned or body or ""):
        return "non_english", cleaned
    if len(cleaned) < MIN_USEFUL_CHARS:
        return "chrome", cleaned
    if cleaned.count("|") >= PIPE_HEAVY:
        return "chrome", cleaned
    if prose_density(cleaned) < MIN_SENTENCES_PER_1K:
        return "chrome", cleaned
    return "keep", cleaned


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write embed_status and the cleaned body")
    parser.add_argument("--show", type=int, default=0, help="sample N of each verdict")
    parser.add_argument("--all", action="store_true",
                        help="triage every article, not only the un-chunked ones")
    args = parser.parse_args()

    await db.init_pool()
    try:
        where = ("" if args.all else
                 "where not exists (select 1 from kb_chunks kc where kc.article_id = ka.id)")
        rows = await db.fetch(
            f"select ka.id::text as id, ka.title, ka.body, ka.doc_type, ka.source_url "
            f"from kb_articles ka {where}")
        print(f"triaging {len(rows):,} articles\n")

        verdicts: Dict[str, List[dict]] = collections.defaultdict(list)
        updates: List[Tuple[str, str, str]] = []
        for row in rows:
            verdict, cleaned = triage(row["title"] or "", row["body"] or "")
            verdicts[verdict].append({**row, "cleaned": cleaned})
            updates.append((verdict, cleaned if verdict == "keep" else None, row["id"]))

        total = max(len(rows), 1)
        print(f"{'verdict':<14}{'rows':>8}   share")
        for verdict in ("keep", "chrome", "non_english"):
            n = len(verdicts[verdict])
            print(f"  {verdict:<12}{n:>8}   {n / total * 100:5.1f}%")

        keep = verdicts["keep"]
        if keep:
            by_type = collections.Counter(r["doc_type"] for r in keep)
            print("\nkept, by doc_type:")
            for t, n in by_type.most_common():
                print(f"   {str(t):<18} {n}")
            chars = sum(len(r["cleaned"]) for r in keep)
            # ~3,200 chars per chunk, matching chunk_text() in embed_corpus.py.
            print(f"\n   text kept: {chars:,} chars  ~ {chars // 3200:,} chunks to embed")
            print(f"   (vs {sum(len(r['body'] or '') for r in rows) // 3200:,} chunks "
                  f"if the whole set were embedded uncleaned)")

        for verdict in ("keep", "chrome", "non_english"):
            for r in verdicts[verdict][: args.show]:
                print(f"\n--- {verdict} · {str(r['doc_type'])} ---")
                print(f"    title  : {str(r['title'])[:66]}")
                print(f"    url    : {str(r['source_url'])[:76]}")
                print(f"    before : {' '.join((r['body'] or '').split())[:150]}")
                print(f"    after  : {r['cleaned'][:150] or '(nothing left)'}")

        if not args.apply:
            print("\nmeasure only — re-run with --apply to write embed_status")
            return 0

        await db.execute("alter table kb_articles add column if not exists embed_status text")
        await db.execute("alter table kb_articles add column if not exists clean_body text")
        await db.execute_many(
            "update kb_articles set embed_status = %s, clean_body = coalesce(%s, clean_body) "
            "where id::text = %s",
            updates,
        )
        print(f"\nwrote embed_status for {len(updates):,} articles")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
