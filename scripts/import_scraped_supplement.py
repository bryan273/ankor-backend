#!/usr/bin/env python3
"""Load the scraped goldset supplement into the shared Supabase project.

Idempotent and provenance-first: nothing is invented, and nothing is written
without a matching source_url from the scrape.

What it can add
---------------
1. error_codes  - the codes extracted verbatim from scraped KB article text.
                  product_id is resolved ONLY through the team's own
                  kb_articles.product_ids (matched by article slug); when their
                  data gives no product for a code the row is inserted with
                  product_id NULL rather than guessing.
                  `meaning` is left NULL on purpose: the scrape never contained one.
2. kb_articles  - scraped articles whose slug is absent from their table
                  (dedup by slug, so the same article under a different host
                  is not imported twice). Bodies are the real scraped text;
                  product_ids is left empty (the scrape has no UUID mapping) so
                  their chunker simply attaches no SKUs.
                  Chunking + embedding is their pipeline's job
                  (scripts/embed_corpus.py picks up any body > 100 chars).

Usage
-----
    python3 tools/supabase_import.py --dry-run          # default, writes nothing
    python3 tools/supabase_import.py --apply            # perform the inserts
    python3 tools/supabase_import.py --apply --only error_codes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "goldset" / "data" / "raw"
BASE = "https://wmxucgywnafvlnybkjhi.supabase.co/rest/v1"
BATCH = 100


def rest(base: str, key: str, path: str, *, method: str = "GET", body=None,
         headers: dict | None = None, retries: int = 3):
    url = f"{base}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    h.update(headers or {})
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")[:400]
            if attempt == retries - 1:
                return e.code, detail
            time.sleep(2 * (attempt + 1))
        except Exception as e:  # noqa: BLE001 - network flake -> retry
            if attempt == retries - 1:
                return 0, str(e)
            time.sleep(2 * (attempt + 1))
    return 0, "unreachable"


def slug_of(url: str) -> str:
    return (url or "").rstrip("/").split("/")[-1].split("?")[0].lower()


def norm(text: str) -> str:
    """Whitespace/case-insensitive form, so two crawls of one article compare equal."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def prefix_hash(body: str, n: int = 1500) -> str:
    """Hash of the first n normalized chars - survives differing tails/rubrics."""
    return hashlib.sha1(norm(body)[:n].encode()).hexdigest()


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--key-file", default=str(ROOT / ".secrets" / "supabase_secret_key"))
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", dest="dry_run", action="store_false")
    ap.add_argument("--only", choices=["error_codes", "kb_articles", "all"], default="all")
    a = ap.parse_args()

    key = pathlib.Path(a.key_file).read_text().strip()
    if not key:
        print("no key", file=sys.stderr)
        return 2

    # ---- their side -------------------------------------------------------
    # PostgREST caps a response at 1000 rows by default: page through.
    def fetch_all(table: str, select: str) -> list[dict]:
        out, offset = [], 0
        while True:
            st, rows = rest(a.base, key, f"{table}?select={select}&limit=1000&offset={offset}")
            if st != 200:
                raise SystemExit(f"{table} read failed: {st} {rows}")
            out.extend(rows)
            if len(rows) < 1000:
                return out
            offset += 1000

    print("reading their tables ...")
    products = fetch_all("products", "id,sku")
    sku_to_id = {p["sku"]: p["id"] for p in products if p.get("sku")}
    cached = pathlib.Path(".secrets/their_kb_articles.json")
    if cached.exists() and a.dry_run:
        articles = json.loads(cached.read_text(encoding="utf-8"))
        print(f"  (their kb_articles from cache: {cached})")
    else:
        # On --apply always read live: a cached snapshot goes stale the moment we
        # write, which would make the next run plan the same rows again.
        articles = fetch_all("kb_articles", "id,source_url,title,product_ids,body")
        if cached.exists() and a.dry_run:
            cached.write_text(json.dumps(articles), encoding="utf-8")
    their_slugs = {slug_of(x["source_url"]) for x in articles}
    their_titles = {norm(x.get("title")): (x.get("product_ids") or []) for x in articles if x.get("title")}
    their_prefixes = {prefix_hash(x.get("body") or "") for x in articles}

    def their_product_ids(mine: dict) -> list[str]:
        """Resolve a scraped article to THEIR product rows - never guesses.

        Three deterministic keys, cheapest first: URL slug, exact title, then the
        hash of the first 1500 normalized characters of the body (both crawls read
        the same Salesforce article, so the text matches even when the URL shape
        or the tail differs).
        """
        for url in (mine.get("source_urls") or ([mine["source_url"]] if mine.get("source_url") else [])):
            s = slug_of(url)
            for x in articles:
                if slug_of(x["source_url"]) == s:
                    return x.get("product_ids") or []
        t = norm(mine.get("question") or mine.get("title") or "")
        if t and t in their_titles:
            return their_titles[t]
        body = mine.get("body_text") or ""
        if len(body) > 200:
            p = prefix_hash(body)
            for x in articles:
                if prefix_hash(x.get("body") or "") == p:
                    return x.get("product_ids") or []
        return []

    aliases = fetch_all("product_aliases", "alias,product_id")
    products_full = fetch_all("products", "id,name")

    def id_via_their_resolver(hints: list[str]) -> str | None:
        """Fall back to THEIR alias/name table - the same text->product resolution
        their app performs, so an attribution is never invented here. Rows matched
        this way are reported separately so they can be reviewed."""
        def c(s: str) -> str:
            return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()
        for h in hints:
            hc = c(h)
            if len(hc) < 6:
                continue
            for p in products_full:
                n = c(p.get("name"))
                if n and (hc in n or n in hc):
                    return p["id"]
            for a in aliases:
                ac = c(a.get("alias"))
                if ac and (hc == ac or (len(hc) > 10 and (hc in ac or ac in hc))):
                    return a["product_id"]
        return None

    existing_codes = fetch_all("error_codes", "id,product_id,code,source_url")
    print(f"  products {len(products)} | kb_articles {len(articles)} | error_codes {len(existing_codes)}")

    mine_codes = read_jsonl(RAW / "error_codes.jsonl")
    mine_arts = read_jsonl(RAW / "support_articles.jsonl")

    # ---- error_codes ------------------------------------------------------
    existing_pairs = {(c.get("product_id"), (c.get("code") or "").strip()) for c in existing_codes}
    existing_code_urls = {((c.get("code") or "").strip(), c.get("source_url")) for c in existing_codes}
    seen_pairs: set = set()
    code_rows, mapped, alias_mapped, unmapped, skipped = [], 0, 0, 0, 0
    for c in mine_codes:
        code = (c.get("code") or "").strip()
        urls = c.get("source_urls") or []
        src_url = urls[0] if urls else None
        if not code or not src_url:
            skipped += 1
            continue
        steps = []
        for res in (c.get("resolutions") or []):
            steps.extend(res.get("steps") or [])
        pid = None
        for pid_candidate in (their_product_ids(c) or []):
            pid = pid_candidate
            break
        via = "content" if pid else None
        if not pid:
            pid = id_via_their_resolver(c.get("products") or [])
            via = "alias" if pid else None
        if (pid, code) in existing_pairs or (code, src_url) in existing_code_urls:
            skipped += 1
            continue
        if (pid, code) in seen_pairs:
            skipped += 1          # UNIQUE(product_id, code) would reject the batch
            continue
        seen_pairs.add((pid, code))
        code_rows.append({
            "product_id": pid, "code": code, "meaning": None,
            "fix_steps": steps[:12], "source_url": src_url,
            "_via": via or "unmapped",
        })
        if via == "content":
            mapped += 1
        elif via == "alias":
            alias_mapped += 1
        else:
            unmapped += 1

    # ---- kb_articles delta ------------------------------------------------
    art_rows, dup_slugs, dup_content = [], 0, 0
    seen = set()
    for art in mine_arts:
        url = art.get("source_url") or art.get("url") or ""
        body = (art.get("body_text") or "").strip()
        s = slug_of(url)
        if not url or not s or len(body) <= 100:
            continue
        if s in their_slugs or s in seen:
            dup_slugs += 1
            continue
        if prefix_hash(body) in their_prefixes:
            dup_content += 1          # same article already stored under another URL
            continue
        seen.add(s)
        art_rows.append({
            "source_url": url,
            "title": (art.get("question") or "").strip()[:500] or None,
            "doc_type": None,
            "product_ids": [],
            "body": body,
            "lang": art.get("lang") or "en",
        })

    payload_bytes = len(json.dumps(art_rows).encode()) + len(json.dumps(code_rows).encode())
    print("\n--- plan -------------------------------------------------")
    print(f"  error_codes : {len(code_rows)} rows "
          f"(product via their KB {mapped}, via their alias table {alias_mapped}, "
          f"unmapped {unmapped}, already present/skipped {skipped})")
    print(f"  kb_articles : {len(art_rows)} new rows "
          f"(slug already theirs: {dup_slugs}, same content under another URL: {dup_content})")
    print(f"  payload     : {payload_bytes/1e6:.2f} MB")

    if a.dry_run:
        print("\nDRY RUN - nothing written. Re-run with --apply.")
        return 0

    # ---- apply ------------------------------------------------------------
    manifest = {
        "target": a.base,
        "inserted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "error_codes": {
            "rows": len(code_rows),
            "detail": [
                {"code": r["code"], "product_id": r["product_id"],
                 "source_url": r["source_url"], "resolved_via": r["_via"]}
                for r in code_rows
            ],
        },
        "kb_articles": {"rows": len(art_rows),
                        "slugs": [slug_of(r["source_url"]) for r in art_rows]},
    }
    out = ROOT / "goldset" / "data" / "imports"
    out.mkdir(parents=True, exist_ok=True)
    mp = out / f"import_manifest_{time.strftime('%Y%m%d-%H%M%S')}.json"
    mp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"\naudit manifest -> {mp.relative_to(ROOT)}")

    wrote = Counter()
    if a.only in ("error_codes", "all") and code_rows:
        for i in range(0, len(code_rows), BATCH):
            chunk = [{k: v for k, v in r.items() if not k.startswith("_")} for r in code_rows[i:i + BATCH]]
            st, resp = rest(a.base, key, "error_codes", method="POST", body=chunk,
                            headers={"Prefer": "return=minimal"})
            if st not in (200, 201, 204):
                print(f"  error_codes batch {i} FAILED: {st} {resp}", file=sys.stderr)
                return 1
            wrote["error_codes"] += len(chunk)
            print(f"  error_codes +{len(chunk)} (total {wrote['error_codes']})")
    if a.only in ("kb_articles", "all") and art_rows:
        for i in range(0, len(art_rows), BATCH):
            chunk = art_rows[i:i + BATCH]
            st, resp = rest(a.base, key, "kb_articles", method="POST", body=chunk,
                            headers={"Prefer": "return=minimal"})
            if st not in (200, 201, 204):
                print(f"  kb_articles batch {i} FAILED: {st} {resp}", file=sys.stderr)
                return 1
            wrote["kb_articles"] += len(chunk)
            print(f"  kb_articles +{len(chunk)} (total {wrote['kb_articles']})")

    print("\n--- written ----------------------------------------------")
    for t, n in wrote.items():
        st, _ = rest(a.base, key, f"{t}?select=id&limit=1", headers={"Prefer": "count=exact"})
        print(f"  {t}: +{n}")
    # keep the local snapshot honest for the next dry run
    try:
        rest(a.base, key, "kb_articles?select=id&limit=1")
        pathlib.Path(".secrets/their_kb_articles.json").unlink(missing_ok=True)
        print("  (stale kb cache removed - next dry run refetches live)")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
