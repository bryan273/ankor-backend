# TASK — schema/code alignment + product dedupe (KC agent ⇄ Bryan agent)

**Status:** open · **Owner:** Bryan's agent (Claude) · **Published by:** KC's agent
**Refs:** this file (`docs/tasks/TASK-schema-dedupe.md`) · shared Supabase `wmxucgywnafvlnybkjhi` · Pinecone `anker-support`

> **This document is temporary.** It is deleted in the final commit of this task (see
> §5 "Close-out"). Until then it is the single coordination point. `git log` is the
> permanent record — write it accordingly (§4).

---

## 0. How to use this document

1. Read §1 (state) and §2–§3 (asks) **before** touching the DB.
2. Answer §6 "Decisions needed" first — two of them change the migration you write.
3. Do the work in small commits; after **every** commit, append one row to the log in §7
   **in the same commit** as the change it describes (so the log can never drift from reality).
4. When the acceptance gates in §3 pass, execute §5 Close-out.

Everything stated as a fact below was measured against the live DB on 2026-09-16. Re-verify
with the commands in §8 rather than trusting prose — if a number differs, the number wins and
you should say so in the log.

## 1. What changed (verified live, before → after)

| table | before | after | delta | pushed by |
|---|---|---|---|---|
| `kb_articles` | 14,665 | **14,799** | +134 policy docs (`doc_type='policy'`, new vocabulary value) | KC agent |
| `products` | 898 | **1,493** | **+595** rows (644 local SKUs total) | KC agent |
| `product_docs` | 1,272 | 1,272 | unchanged | KC agent |
| `error_codes` | 139 | 139 | unchanged | KC agent |

`kb_articles` `doc_type` distribution now: `article` 10,028 · `faq` 2,526 · `manual` 1,247 ·
`troubleshooting` 864 · **`policy` 134** (sums to 14,799 ✓).

Corpus provenance: deterministic scrape (trafilatura) over anker/eufy/soundcore product,
support, policy and doc surfaces; no LLM-generated content; every row carries `source_url`.
KC's agent owns text freshness in `kb_articles`; you own `products`, `dealers`,
`warranty_policies`, the embedding pipeline and the app contracts.

## 2. The dedupe problem (main ask)

The SKU spaces genuinely differ: **only 49 of KC's 644 SKUs overlapped your 898 before the
push**, yet the catalogs describe the same products. Mechanism, now confirmed:

**Your SKUs encode region + bundle; KC's encode the base SKU with the region in the URL.**

```
slug=a1614
  his  sku=A1614021   https://www.anker.com/products/a1614
  MINE sku=A1614011   https://www.anker.com/au/products/a1614
```

Measured on live data:

- **112 slug groups contain more than one row** — 240 rows involved → **128 excess rows**.
- **All 112 mixed groups contain at least one KC-pushed row** → this is a merge problem, not a
  pre-existing one.
- Only **3** groups lack any canonical / `BUNDLE-` / `COMBO-` member.
- Your special families: `BUNDLE…` 339 · `COMBO…` 4 · `B…`-prefixed 494.
- Exact-name collisions: **167** same-name/different-SKU pairs (a superset — names drift across
  locales, so **slug is the better key**).
- Residue: **16** rows NULL `category` · **46** NULL `price` · 2 NULL `slug`.

So "remove duplicates" is really: **collapse region/bundle variants onto a canonical product and
keep the variants as aliases**, not delete the fact that they exist.

## 3. Tasks and acceptance gates

### 3.1 Canonical / alias model (highest value)

Implement an **idempotent SQL migration**:

- Suggested key: **normalised product slug** (strip locale path segments, lowercase) → one
  canonical row. Prefer the non-`BUNDLE`/`COMBO`/`B…` base SKU, or the global
  `anker.com/products/…` URL over a regional one (your call — see §6).
- Non-canonical variants (regional SKUs, `BUNDLE-*`, `COMBO-*`) become rows in
  **`product_aliases`** (currently 1,396 rows) pointing at the canonical product, so SKU
  resolution keeps working for every variant.
- Decide the fate of non-canonical `products` rows (keep with a `status` marker vs delete after
  alias-ification). **Never delete a row without first proving an alias exists for its SKU.**
- If `product_aliases` can't express *why* (region vs bundle), propose a column rather than
  overloading it.

**Gate:** every SKU that existed pre-migration still resolves
(`products.sku` → `product_aliases` → product). **Zero orphaned SKUs.** Report excess rows
collapsed, aliases created, rows deleted (with the proof they were alias-safe).

### 3.2 Schema alignment — three mismatches found while pushing

- **`product_docs` (1,272 rows) is never read by `scripts/embed_corpus.py`.** Text column is
  `parsed_text` (not `text`); columns: `id, product_id, kind, title, url, local_path,
  parsed_text, pages, fetched_at`. These are manual/PDF parses — exactly the material for
  fault-localisation — and are currently **inert for retrieval**. Either add an
  `embed_product_docs()` surface or migrate them into `kb_articles` with a dedicated
  `doc_type`. State which and why.
- **`dealers` was deliberately NOT pushed.** KC's 56 records are where-to-buy/service links
  (`source_host, source_url, name, domain, outbound_url, channel`); your table is curated dealer
  records (`name, region, order_no_pattern, contact, service_path, authorized, source`, 24 rows).
  Decide: extend the schema, map a defined subset, or keep them out (and say so, so KC drops
  them from the push set). Do not bulk-insert.
- **`warranty_policies` was deliberately NOT pushed.** Yours is category-level
  (`category, months, covers, excludes`; 5 rows); KC's 138 policy **document** URLs went to
  `kb_articles` as `doc_type='policy'` (134 inserted, 4 already present under another
  `doc_type`). Confirm the home; if the category-level table should be seeded from those
  documents, define a deterministic extraction rule cited to `source_url`.

Also verify the retrieval contract for the new value: `app/services/kb.py` filters only when a
caller passes `doc_type` (~line 77), so unfiltered search must still return `policy` rows — and
add `policy` to any UI/doc-type filters.

### 3.3 Fix the embedding request pattern (biggest throughput lever)

`embed_batch()` in `scripts/embed_corpus.py` looks batched (`BATCH = 32`) but calls
`embed_many(texts, ..., concurrency=8)` → **32 separate API requests, one per chunk**. So
~120k pending chunks ≈ ~120k requests ≈ **months** on the free tier.

Switch to a real batched call (`batchEmbedContents`, up to 100 contents per request) while
preserving: model `gemini-embedding-001`, **3072-d** output (index `anker-support` is
3072/cosine — do not change dimension), `taskType=RETRIEVAL_DOCUMENT`, and the
`kb_chunks(article_id, ord, text_hash, pinecone_id, embedded_at)` ledger semantics so the job
stays **resumable and idempotent**.

Quota discipline is a hard constraint, not a preference: single key, single project, hard daily
request budget, exponential backoff on 429, no retry storms. A previous Google account was
banned for exactly that.

**Gate:** chunks/minute before vs after, plus a bounded pilot (e.g. 500 chunks) proving
`kb_chunks` rows ↔ Pinecone vectors 1:1.

### 3.4 Backfill residue

16 NULL `category` → run/extend `scripts/backfill_categories.py`; 46 NULL `price` →
`scripts/backfill_prices.py`, or mark them explicitly price-unknown rather than leaving NULL
ambiguous.

## 4. Git protocol (binding for both agents)

1. **Every action is a commit.** No uncommitted work left at the end of a work session; commit
   at each logical step (schema change, script change, backfill run, verification).
2. **Commit message format** — the permanent record, so it must stand alone:

   ```
   <type>(<scope>): <imperative summary>

   Task: TASK-schema-dedupe
   What: <files/tables touched, and the change in one sentence each>
   Counts: <before → after, per table or per SKU family>
   Ran: <the exact commands / SQL applied, verbatim or by committed filename>
   Verified: <the verification output that proves it, e.g. "orphan SKUs: 0">
   Not done: <anything deliberately skipped, + why>
   ```

3. **Append the matching row to §7 in the same commit** as the change. A log row without a
   commit, or a commit without a log row, is a protocol violation.
4. **Push to `origin/main`; never force-push main.** `git fetch` + rebase before pushing. If a
   conflict appears, resolve it and log the resolution (that's information, not a nuisance).
5. **SQL is the source of truth for DB changes.** Never leave a state that exists only in the
   dashboard editor: the migration goes in `sql/` and is committed, with the applied state
   recorded in §7.
6. **No secrets in commits.** Keys stay in `.env` (gitignored). No large dumps — counts and a
   few representative samples only.
7. **Record what you chose NOT to do**, with the reason. Silent omissions are the main source of
   cross-agent miscommunication.

## 5. Close-out

When §3.1–§3.4 gates pass:

1. Add a final §7 row summarising the whole task (final counts, gates, artifacts).
2. Delete this file (`git rm docs/tasks/TASK-schema-dedupe.md`) in a commit titled
   `chore: remove completed task doc — schema/dedupe (see git log for the record)` whose body
   carries the final counts and the acceptance evidence.
3. Remove any now-dangling references to this file from README/docs in the same commit.
4. Reply to KC with: the deletion commit hash, the final counts, and the list of artifacts
   (migration + script diff + report).

The git history — not this file — is what survives, which is exactly why §4 exists.

## 6. Decisions needed before coding

1. **Canonical = base SKU, or canonical = the global-URL row regardless of SKU?** KC's agent
   used slug-normalisation as the candidate key; you own the decision.
2. **Do regional variants need to stay queryable with their own price/currency** (e.g. `AUD`
   rows)? If yes, alias-per-region may be wrong and a `region` column on `products` may be right.
3. **`BUNDLE-*` / `COMBO-*`** — are these products in your model, or offers that never should
   have been catalog rows?
4. **Is `product_docs` meant to be a retrieval surface at all**, or storage for the vision lane?

## 7. Handoff log (append-only — one row per commit)

| date (UTC) | agent | commit | action | counts / evidence | verified by |
|---|---|---|---|---|---|
| 2026-09-16 | kc-agent | `394d566` (workspace) | scraped the remaining content gaps → durable Supabase sink | `kb_articles` → 14,665 · `product_docs` → 1,272 · `error_codes` → 139 | live counts via REST |
| 2026-09-16 | kc-agent | `e6dcf1e` (workspace) | extract-and-discard scraper mode + Parquet output | — | Colab run receipts |
| 2026-09-16 | kc-agent | `426e21a` (workspace) | idempotent text push (`push_to_supabase.py`, now shipped in this repo) | `products` 898 → **1,493** (+595) · `kb_articles` 14,665 → **14,799** (+134 `policy`) | `--what all --dry-run` → `to_push: 0`; independent MECE: 0 missing SKUs |
| 2026-09-16 | kc-agent | `2b58669` (workspace) | authored this handoff | — | this document |
| 2026-09-16 | kc-agent | `(the commit that adds this file)` (backend) | published this task doc to `docs/tasks/` | — | `git log` on `backend` |
| 2026-09-16 | kc-agent | `(this commit)` (backend) | shipped the tool into this repo + added a `--coverage` mode | `--coverage` reproduces §2 verbatim: 112/240/128 · 16/46/2 · kb 14,799 · policy 134 | ran from this repo with no local scrape files; key resolved from `.env` |

**kc-agent notes for the push session (so nothing is re-derived):**

- `products.brand` is **NOT NULL** — the crawler leaves it null on some soundcore/eufy templates;
  a deterministic fallback from the URL host was applied before insert.
- PostgREST needs **`on_conflict=<key>`** for merge-upserts, else you get `409 duplicate key`
  instead of a merge (`products` key = `sku`; `kb_articles` key = `source_url`).
- PostgREST **caps a single response** (`db-max-rows`) — a naive `select` truncated at 1,000 rows
  and made an early coverage check report 441 false gaps. Always paginate (§8).

## 8. Repro commands

```bash
# ONE-COMMAND EVIDENCE for §2 / §3.1 / §3.2 — runs against the live DB, needs no local files.
# Reproduces §2 verbatim: 112 duplicate slug groups / 240 rows / 128 excess; 16 no_category;
# 46 no_price; 2 no_slug; kb_articles 14,799 with doc_type='policy' 134.
python3 scripts/push_to_supabase.py --coverage
```

```sql
-- canonical/alias candidates
select lower(slug) as slug, count(*) as rows, array_agg(sku order by sku) as skus
from products where slug is not null
group by 1 having count(*) > 1 order by 2 desc;

-- residue
select count(*) filter (where category is null) as no_category,
       count(*) filter (where price is null)    as no_price,
       count(*) filter (where slug  is null)    as no_slug
from products;

-- retrieval contract for the new doc_type
select doc_type, count(*) from kb_articles group by 1 order by 2 desc;
```

```bash
# always paginate: PostgREST caps db-max-rows, so single-shot counts lie
for off in 0 1000 2000; do
  curl -s "https://wmxucgywnafvlnybkjhi.supabase.co/rest/v1/products?select=sku&limit=1000&offset=$off" \
    -H "apikey: $SUPABASE_SECRET_KEY" -H "Authorization: Bearer $SUPABASE_SECRET_KEY"; done

# the push surfaces (dry-run default; idempotent). Inputs are KC-side scrape JSONL — absent
# in this repo, in which case the local-row counts report 0 and nothing is written.
SCRAPE_INPUT_DIR=<path-to-scrape-jsonl> python3 scripts/push_to_supabase.py --what all --dry-run
```

## 9. Non-goals (unchanged agreement)

- Do not rewrite `kb_articles` text — freshly scraped, deterministic; only `doc_type`
  classification is yours to adjust.
- Do not change PostgREST contracts the push relies on: `kb_articles` unique = `source_url`,
  `products` unique = `sku`, merge-upserts require `on_conflict`.
- Do not re-embed unchanged chunks — the `text_hash` skip logic must survive your batching change.
