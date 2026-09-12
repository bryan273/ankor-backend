#!/usr/bin/env python3
"""Backfill doc_type on kb_articles rows that have none (the scraped import).

Their retrieval filters on doc_type (`app/services/kb.py` -> vector_search(doc_type=...)),
so a NULL doc_type row is silently unreachable in those lanes. This applies the
team's OWN classification rule - copied verbatim from
scripts/crawl_support.py::doc_type_for - to the slug, so imported rows are
classified exactly like rows their crawler writes. Nothing is invented.

    python3 tools/backfill_doc_type.py --dry-run     # default
    python3 tools/backfill_doc_type.py --apply
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import urllib.error
import urllib.request
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://wmxucgywnafvlnybkjhi.supabase.co/rest/v1"
BATCH = 80

# verbatim from backend/scripts/crawl_support.py
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


def slug_of(url: str) -> str:
    return (url or "").rstrip("/").split("/")[-1].split("?")[0]


def req(method: str, path: str, key: str, body=None, headers=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    h.update(headers or {})
    r = urllib.request.Request(f"{BASE}/{path}", data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:300]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file", default=str(ROOT / ".secrets" / "supabase_secret_key"))
    ap.add_argument("--apply", dest="dry_run", action="store_false")
    ap.add_argument("--dry-run", action="store_true", default=True)
    a = ap.parse_args()
    key = pathlib.Path(a.key_file).read_text().strip()

    rows, off = [], 0
    while True:
        st, chunk = req("GET", f"kb_articles?select=id,source_url,doc_type&doc_type=is.null&limit=500&offset={off}", key)
        if st != 200:
            raise SystemExit(f"read failed: {st} {chunk}")
        rows.extend(chunk)
        if len(chunk) < 500:
            break
        off += 500

    plan = [(r["id"], doc_type_for(slug_of(r["source_url"])), slug_of(r["source_url"])) for r in rows]
    print(f"rows with doc_type NULL: {len(plan)}")
    print("  would become:", dict(Counter(t for _, t, _ in plan)))
    if a.dry_run:
        print("DRY RUN - nothing written. Re-run with --apply.")
        return 0

    # PATCH by id list, batched
    done = Counter()
    for i in range(0, len(plan), BATCH):
        batch = plan[i:i + BATCH]
        by_type: dict[str, list[str]] = {}
        for rid, t, _ in batch:
            by_type.setdefault(t, []).append(rid)
        for t, ids in by_type.items():
            st, body = req("PATCH", f"kb_articles?id=in.({','.join(ids)})", key,
                           body={"doc_type": t}, headers={"Prefer": "return=minimal"})
            if st not in (200, 204):
                print(f"  batch {i} ({t}) FAILED: {st} {body}")
                return 1
            done[t] += len(ids)
        print(f"  patched {min(i + BATCH, len(plan))}/{len(plan)}", flush=True)

    print("updated:", dict(done))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
