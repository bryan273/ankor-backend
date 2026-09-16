#!/usr/bin/env python3
"""Push locally-scraped text into the shared Supabase project.

Idempotent + resumable: every surface is keyed on its natural key and pushed with
resolution=merge-duplicates, so re-running is safe and only the delta moves.

Surfaces
  policies  -> kb_articles  (doc_type='policy')   key: source_url
  products  -> products                            key: sku  (dedup-aware, see --new-only)

Usage
  python3 tools/push_to_supabase.py --what policies --dry-run
  python3 tools/push_to_supabase.py --what policies --apply
  python3 tools/push_to_supabase.py --what products --new-only --apply
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
def _secret() -> str:
    """Resolve the Supabase secret key: env -> .secrets/ -> this repo's .env.
    Bryan's repo has it in .env, KC's box has it in .secrets/."""
    for var in ("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        if os.environ.get(var):
            return os.environ[var].strip()
    p = ROOT / ".secrets" / "supabase_secret_key"
    if p.exists():
        return p.read_text().strip()
    envf = ROOT / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if line.startswith(("SUPABASE_SECRET_KEY=", "SUPABASE_SERVICE_ROLE_KEY=")):
                return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("no Supabase secret: set SUPABASE_SECRET_KEY, or add .env / .secrets/supabase_secret_key")


KEY = _secret()
BASE = "https://wmxucgywnafvlnybkjhi.supabase.co/rest/v1"

# Inputs are the local scrape output (KC-side). In this repo they may be absent — that is
# fine: `--coverage` needs no local files, and the push surfaces simply report 0 local rows.
INPUT_DIR = pathlib.Path(os.environ.get("SCRAPE_INPUT_DIR", "data/scraped"))

POLICY_FILES = ["goldset/data/raw/policies.jsonl", "goldset/colab/colab_out/policies.jsonl"]
PRODUCT_FILES = [
    "goldset/data/raw/products.jsonl",
    "goldset/colab/colab_out/products.jsonl",
    "goldset/colab/colab_out/canonical_products.jsonl",
]


def _req(path: str, *, method: str = "GET", body=None, prefer: str | None = None, extra_headers=None):
    headers = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}/{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode() or "[]"
            return json.loads(raw) if raw.strip().startswith(("[", "{")) else raw
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} on {path}: {e.read().decode()[:400]}") from None


def load_jsonl(paths: list[pathlib.Path]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def resolve_inputs(names: list[str]) -> list[pathlib.Path]:
    """Look for each scrape file under --input-dir, then legacy KC-side locations."""
    out: list[pathlib.Path] = []
    for n in names:
        for cand in (INPUT_DIR / n,
                     ROOT / "goldset" / "data" / "raw" / n,
                     ROOT / "goldset" / "colab" / "colab_out" / n):
            if cand.exists() and cand not in out:
                out.append(cand)
                break
    return out


def dedupe(rows: list[dict], key):
    out: dict = {}
    for r in rows:
        k = key(r)
        if k:
            out[k] = r
    return out


def _req_all(path: str, page: int = 1000) -> list[dict]:
    """Page through a table: PostgREST caps a single response (db-max-rows), which
    silently truncates naive SELECTs and makes coverage checks lie."""
    out: list[dict] = []
    offset = 0
    while True:
        sep = "&" if "?" in path else "?"
        batch = _req(f"{path}{sep}limit={page}&offset={offset}")
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return out


def existing_source_urls(urls: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(urls), 100):
        batch = urls[i : i + 100]
        q = urllib.parse.urlencode({"source_url": f"in.({','.join(batch)})", "select": "source_url"})
        for r in _req(f"kb_articles?{q}"):
            found.add(r["source_url"])
    return found


def brand_guess(url: str, title: str) -> str:
    """Deterministic brand from the product URL host (the crawler leaves it null for
    some soundcore/eufy templates, and products.brand is NOT NULL)."""
    u = (url or "").lower()
    host = urllib.parse.urlparse(u).netloc
    if "soundcore" in host:
        return "soundcore"
    if "eufy" in host:
        return "eufy"
    if "anker" in host:
        return "Anker SOLIX" if "/solix" in u else "Anker"
    t = (title or "").lower()
    if "soundcore" in t:
        return "soundcore"
    if "eufy" in t:
        return "eufy"
    if "solix" in t:
        return "Anker SOLIX"
    if "anker" in t:
        return "Anker"
    return "Unknown"


def push_policies(apply: bool) -> dict:
    rows = dedupe(load_jsonl(resolve_inputs(["policies.jsonl"])), lambda r: r.get("url"))
    payload = [
        {
            "source_url": r["url"],
            "title": (r.get("title") or "")[:500],
            "doc_type": "policy",
            "body": r.get("body_text") or "",
            "lang": "en",
        }
        for r in rows.values()
        if (r.get("body_text") or "").strip()
    ]
    live = existing_source_urls([p["source_url"] for p in payload])
    todo = [p for p in payload if p["source_url"] not in live]
    stats = {"local_unique": len(rows), "with_text": len(payload), "already_live": len(live), "to_push": len(todo)}
    if apply and todo:
        for i in range(0, len(todo), 100):
            _req("kb_articles?on_conflict=source_url", method="POST", body=todo[i : i + 100],
                 prefer="resolution=merge-duplicates,return=minimal")
        stats["pushed"] = len(todo)
    return stats


def push_products(apply: bool, new_only: bool) -> dict:
    rows = dedupe(load_jsonl(resolve_inputs(["products.jsonl", "canonical_products.jsonl"])),
                  lambda r: (r.get("sku") or "").upper().replace(" ", ""))
    live = _req_all("products?select=sku,name,brand")
    live_skus = {(p.get("sku") or "").upper().replace(" ", "") for p in live}
    live_names = {(p.get("name") or "").strip().lower() for p in live}

    payload, dupes_name, dupes_sku = [], 0, 0
    for norm_sku, r in rows.items():
        if norm_sku in live_skus:
            dupes_sku += 1
            continue
        if new_only and (r.get("title") or "").strip().lower() in live_names:
            dupes_name += 1
            continue
        url = r.get("url") or ""
        payload.append({
            "sku": r.get("sku"),
            "name": (r.get("title") or "")[:500],
            "brand": r.get("brand") or brand_guess(url, r.get("title")),
            "url": url,
            "slug": url.rstrip("/").split("/")[-1][:200] or None,
            "price": r.get("price") if isinstance(r.get("price"), (int, float)) else None,
            "currency": r.get("currency"),
            "hero_image": r.get("image") or r.get("hero_image"),
            "status": "active",
            "raw": r,
        })
    stats = {"local_unique_skus": len(rows), "already_live_by_sku": dupes_sku,
             "skipped_same_name_as_live": dupes_name, "to_push": len(payload)}
    if apply and payload:
        pushed = 0
        for i in range(0, len(payload), 100):
            try:
                _req("products?on_conflict=sku", method="POST", body=payload[i : i + 100],
                     prefer="resolution=merge-duplicates,return=minimal")
                pushed += len(payload[i : i + 100])
            except SystemExit as e:  # one bad batch must not abort the rest
                stats.setdefault("batch_errors", []).append(f"batch@{i}: {str(e)[:160]}")
        stats["pushed"] = pushed
    return stats


def coverage() -> dict:
    """Live-side evidence report — the acceptance evidence for TASK-schema-dedupe §3.1/§3.2.
    Needs no local scrape files."""
    prods = _req_all("products?select=sku,slug,category,price,brand")
    from collections import defaultdict
    by_slug: dict = defaultdict(list)
    for p in prods:
        s = (p.get("slug") or "").strip().lower()
        if s:
            by_slug[s].append(p)
    dups = {s: v for s, v in by_slug.items() if len(v) > 1}
    arts = _req_all("kb_articles?select=doc_type")
    from collections import Counter
    return {
        "products": len(prods),
        "duplicate_slug_groups": len(dups),
        "rows_in_duplicate_groups": sum(len(v) for v in dups.values()),
        "excess_rows": sum(len(v) - 1 for v in dups.values()),
        "no_category": sum(1 for p in prods if p.get("category") is None),
        "no_price": sum(1 for p in prods if p.get("price") is None),
        "no_slug": sum(1 for p in prods if not p.get("slug")),
        "kb_articles": len(arts),
        "kb_doc_type": dict(Counter(a.get("doc_type") or "NULL" for a in arts).most_common()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--what", choices=["policies", "products", "all"], default="all")
    ap.add_argument("--apply", action="store_true", help="default is dry-run")
    ap.add_argument("--dry-run", action="store_true", help="explicit no-op default (kept for symmetry)")
    ap.add_argument("--new-only", action="store_true",
                    help="products: skip rows whose name already exists live (SKU-space mismatch guard)")
    ap.add_argument("--coverage", action="store_true",
                    help="live-side evidence report (duplicate slug groups, residue, doc_type) — needs no local files")
    a = ap.parse_args()
    mode = "APPLY" if a.apply else "DRY-RUN"
    print(f"mode={mode} what={a.what}")
    if a.coverage:
        print("  coverage:", json.dumps(coverage(), indent=2))
        return 0
    if a.what in ("policies", "all"):
        print("  policies ->kb_articles:", json.dumps(push_policies(a.apply)))
    if a.what in ("products", "all"):
        print("  products ->products   :", json.dumps(push_products(a.apply, a.new_only)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
