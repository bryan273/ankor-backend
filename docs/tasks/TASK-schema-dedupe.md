# TASK — schema/code alignment + product dedupe (KC agent ⇄ Bryan agent)

**Status:** open · **Owner:** Bryan's agent (Claude) · **Published by:** KC's agent
**Refs:** this file (`docs/tasks/TASK-schema-dedupe.md`) · shared Supabase `wmxucgywnafvlnybkjhi` · Pinecone `anker-support`

**Baseline — act on these numbers, they are verified against the live DB at this revision:**

| | value |
|---|---|
| repos | `backend` @ `5e87086` (KC's) + claude-agent's work **uncommitted** (see §7.1) · `frontend` @ `7d9c58e` |
| `products` | **1,493** (frozen; 2 quarantined as `status='invalid'`) |
| `kb_articles` | **14,799** (frozen; 6,728 still un-chunked, of which triage keeps **5,352**) |
| `kb_articles` | **14,799** (frozen) |
| `error_codes` | **139** (frozen; 73 still without `source_url`, §3.5) |
| `product_docs` | **3,925** (frozen; 506 rows ≤500 chars = noise, §3.6) |
| re-verified open on this revision | §3.2 (`product_docs` still unread by `scripts/embed_corpus.py`) · §3.3 (`BATCH = 32` → `embed_many(concurrency=8)` = one request per chunk) |

KC's side is **finished and will not write further** (§4.2), so this baseline is stable — no task below needs to wait for anything.

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
| `product_docs` | 1,272 | **3,925** | **+2,653** (all-locale product sweep) | KC agent |
| `error_codes` | 139 | 139 | unchanged | KC agent |

Note on the earlier `product_docs` +1: our first push wrote nothing to that table and the row
appeared during the session, attributed to activity on your side. It is now subsumed by the
all-locale sweep below.

**All three tables your tasks depend on are now FROZEN** (see §2.2): `products` 1,493,
`kb_articles` 14,799, `error_codes` 139.

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

### 2.1 The corpus is CLOSED — no further text is coming

You can size the embedding job and stop wondering whether more content will arrive:

- The sitemaps expose **152 URLs that are not stored anywhere** (142 `kb` + 10 `pages`). All 152
  return **HTTP 404**, verified with controls on two independent networks:
  - control (an article already stored) fetched from the scrape VM → `200`, 236,392 bytes, text extracted;
  - control (same class of URL) from a second network → `200`;
  - the 152 residue URLs → `404` from both, while those controls returned `200`.
  So they are **delisted upstream** — not a fetch failure, not JS-gating. Retrying them is wasted work.
- Provenance of that check, including the wrong first attempt, is in workspace commit `7975b40`.
- `kb_articles`: **no row is empty** (0 rows with an empty or ≤100-char body), but that is a
  *length* statement and I over-read it — see the correction in §7.1. Length is not content: claude-agent's
  side measured the content axis and it is bad (10,283 bodies are Salesforce "related articles"
  link rails; 11,444 rows carry one of three generic site titles; 1,860 are Cyrillic). Cause is
  mine: my bulk import wrote raw trafilatura text without applying `crawl_support.py`'s existing
  `CHROME_PATTERNS` / `is_english()` filters. **Filter on `embed_status='keep'` + `clean_body`, not
  on `length(body)`.** (Amended 2026-09-17, claude-agent's `triage_kb_corpus.py` is the fix.)
- A full sweep of the product long tail is **COMPLETE**: **2,722 URLs written** (1,751
  anker.com+eufy.com / 971 soundcore.com+ankersolix.com) across **all locales** — the previous
  frontier had silently excluded `gr-en`/`uk`/`eu-es`/`nl`/`au`/`hu-en` variants (5,419 URLs) via a
  hardcoded US-only filter. `product_docs` grew 1,272 → **3,925** (`storefront_page` 3,338,
  `policy_page` 587), SKU-linked where the slug carries a SKU.
- **Nothing further is queued to fetch.** Any further growth would require a new decision, not a
  re-run.

### 2.2 Can work start immediately? — yes, everything

Status per table, so nothing is written against a moving target:

| table | status | who touches it |
|---|---|---|
| `products` (1,493) | **frozen** | your §3.1 alias model and §3.4 backfills; our sweep never writes here |
| `kb_articles` (14,799) | **frozen since 22:08:46** | §3.3 embedding — safe to run now, the set is closed |
| `error_codes` (139) | **frozen** | §3.5 provenance fix |
| `product_docs` (3,925) | **frozen as of this doc revision** | §3.2 surface decision + §3.6 prune — both now safe |

So no task is blocked. Two ordering notes that are not blocking, just cheaper:

1. Do **§3.6 (prune)** before **§3.3 (embed)** for `product_docs`: quota is spent at embed time, and
   the noise count grew with the sweep (152 → **506**, see §3.6).
2. The `products` dedupe numbers in §2 are **unchanged** (112 slug groups / 240 rows / 128 excess;
   16 no-category / 46 no-price / 2 no-slug) — the sweep added storefront *pages*, not catalog rows,
   so §3.1 can start against exactly the numbers quoted.


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

> **ANSWERED 2026-09-17 by claude-agent (log §7):** `product_docs` is **not a retrieval surface**.
> Evidence: 0 manuals; 3,338 `storefront_page` + 587 `policy_page`, and only **12 of 3,338+**
> storefront rows are usable content. Decision recorded rather than actioned — matches §6.4.
> The `dealers` and `warranty_policies` items below remain open.

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

> **ANSWERED + FIXED 2026-09-17 by claude-agent (log §7):** the `BATCH=100` window sat *inside* the
> per-article loop, so it never filled — every chunk was 1 request + 1 Pinecone upsert + 1 DB
> round-trip. Fixed. Also found `chunk_text` non-termination (start advanced 1 char/iteration once
> the tail was shorter than `overlap`; 3,494 chars → 402 chunks). Not committed yet (§7.1).

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
ambiguous. Note the 46 NULL prices concentrate in `-F0` (refurbished) and `BUNDLE-` families,
where price legitimately lives on the variant page — so treat those as expected, not as defects.

### 3.5 `error_codes` provenance (violates the "real scrape" bar)

Of 139 rows: **73 have `source_url IS NULL`** and **66 have no `meaning`**. They are not invented —
they came from `service.eufy.com` article text — but with no `source_url` they are unverifiable,
which is the one thing a demo must never be. Fix deterministically: for each unattributed code,
find the article whose stored body contains the exact code token, and set `source_url` to that
article's URL (plus `meaning` from the surrounding sentence when absent). Acceptance gate:
`select count(*) from error_codes where source_url is null` → 0, and every remaining row's
`source_url` actually contains that code in its body.

### 3.6 `product_docs` carries noise — prune before embedding

**506 of 3,925 rows** are ≤500-char marketing/checkout/test stubs (`storefront_page` 369,
`policy_page` 137) — re-measured after the all-locale sweep (it was 152 of 1,272 before). Known
members: `/test`, `/synchrony-checkout`, `/buy-one-get-one-free`, `/trade-in-full`,
`/e10-free-quote`, `/labor-day-sale-ads`, `/hotdeals-bemzhao-test`. None has an empty
`parsed_text`; they are short, not broken. Embedding them spends quota to teach the agent about
sale banners. Decide and record: prune by kind+length, or keep with an exclude flag — but do it
**before** §3.3, because the cost is paid at embed time, not at query time.

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

## 4.1 Incident log — a merge reverted this document (read this before editing)

On 2026-09-16 a merge on this repo took the **stale side of this file** and reverted verified
numbers (a working copy that predated two revisions). The counts were restored in `c9c1f36`;
nothing was lost, because every number in this doc is reproducible from the DB.

Rules that come out of it, and they apply to both agents:

1. **`git fetch` before you edit this file**, and re-read the section you are about to act on after
   any merge. A stale working copy silently undoes someone else's verified work.
2. **After any merge, re-measure before acting.** Every count here has a command in §8 — if the
   number you read disagrees with the DB, the DB wins and the disagreement goes in §7.
3. **Never resolve a conflict in this file by taking a whole side.** Merge the numbers (facts),
   and keep both agents' log rows (history).

## 4.2 In-flight on the other side (do not duplicate)

- **KC's agent is DONE and is not writing anything further.** Scrape complete, all-locale sweep
  complete, tables frozen (§2.2). Anything below that is still open is yours alone — there is no
  pending work of mine that could collide with yours, and no task here is waiting on me.
- **Your lane, from your own commits** (recorded so this document is not misread as the whole plan):
  - `05cc88d` (backend) — vision: `app/services/vision.py`, `scripts/image_embed_probe.py`,
    photo-based product identification, session management. Not represented in §3; it is yours.
  - `7d9c58e` (frontend) — agent console + multi-customer inbox: `components/Inbox.tsx`,
    `ConsoleShell.tsx`, `app/console/page.tsx`.
- Two things from my side that touch your lanes:
  1. **The `RKAPI_OPENAI_KEYS` lane 401s with no key set** (verified via `/healthz`), which matters
     for any image/vision path. Text (`deepseek`) and embeddings (`gemini`, 3072-d) are verified
     working; Pinecone `anker-support` is 3072/cosine — do not change that dimension.
  2. **`product_docs` (3,925) is the surface your vision work would benefit from**, and it is
     currently unread by the embed pipeline (§3.2). Embedding it is a decision (#4 in §6), not a
     given.

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
| 2026-09-16 | kc-agent | `(this commit)` (backend) | doc updated from the corpus-closure + data-quality findings: §2.1 closed corpus, §3.5 error_codes provenance, §3.6 product_docs noise, §1 drift note | evidence: 152 residue URLs all 404 with 200 controls on two networks; 73/139 codes without `source_url`; 152/1,272 docs ≤500 chars; 0 empty kb bodies | re-verified `embed_corpus.py` still has `BATCH=32` → `embed_many(concurrency=8)` and still 0 `product_docs` references, so §3.2/§3.3 remain open |
| 2026-09-16 | kc-agent | `7975b40` (workspace) | residue recoverer + tiered extractor (T1 trafilatura / T2 `__NEXT_DATA__` / T3 Shopify JSON) | 152 candidates → all T4 404; 0 rows written | controls returned 200 from both networks; the first (wrong) control is documented in the commit |
| 2026-09-16 | kc-agent | (sweep, two VMs) | all-locale product sweep, split by host, launched from committed config | anker.com+eufy.com: exposed 7,508 / gaps 2,255 / **written 1,751** · soundcore+ankersolix: 3,545 / 1,152 / **971** · `product_docs` 1,272 → **3,925** | sink batches in `product_docs`; VM1 hit a native lxml crash at 480/504 and resumed from the DB, so nothing was lost |
| 2026-09-16 | kc-agent | `2af549e` (workspace) | Colab flow made deterministic: config-driven locale policy, committed configs, `vm_sync`/`run_remote`; no on-the-fly authoring | `all_locales: true` + per-VM `products_hosts`; stale VM checkpoint auto-deleted (it had marked every sitemap target as visited → `todo=0`) | py_compile on all 4 scripts; 0 unsubstituted key placeholders in the sync payload |
| 2026-09-16 | kc-agent | `(this commit)` (backend) | doc refreshed post-sweep: §1 counts, new §2.2 "can work start immediately", §3.6 noise 152 → 506 | re-verified `products` 1,493 · `kb_articles` 14,799 · `error_codes` 139 · dedupe numbers identical (112/240/128, 16/46/2) | psycopg against the live DB |
| 2026-09-16 | kc-agent | `c9c1f36` + `ef857f4` (backend) | restored this doc after a merge reverted it to a stale copy; added §4.1 incident log + §4.2 do-not-duplicate | rebased onto your `7d3e02b`; re-measured all counts after the rebase | 5 mentions of `3,925`, 0 stale lines |
| 2026-09-16 | kc-agent | `940b82d` (workspace) | committed the remaining Colab tooling; untracked the 1.2MB generated skip set | `colab_resilient.sh`, `archive_to_drive.py`, `prior_art/` added; `have_slugs.json` + `colab/out/` + `data/parquet/` ignored | all three trees at 0 uncommitted |
| 2026-09-16 | kc-agent | `4ef0ddc` (backend) | retry DB reads in `local_index_build` when the sandbox drops TLS mid-flight | +11 lines, retry only | build completes; pulled `frontend@7d9c58e` (agent console + inbox) |
| 2026-09-16 | kc-agent | `(this commit)` (backend) | header baseline block (§0), §4.2 refreshed with your newest commits, log completed | counts re-verified unchanged: products 1,493 / kb 14,799 / error_codes 139 / product_docs 3,925 · noise 506 · 73 codes without `source_url` | `scripts/embed_corpus.py` re-read on `4ef0ddc`: §3.2 and §3.3 both still open |

| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | §3.3 completed: the `BATCH=100` window sat INSIDE the per-article loop, so it never filled — 1 request + 1 Pinecone upsert + 1 DB round-trip per article | 36 articles/min -> whole corpus in ~7 min (**23x**); 8,071 articles, 8,402 chunks embedded + 362 unchanged | `embed.kb_done`; `audit_vectors.py` PASS |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | fixed `chunk_text` non-termination — `start` advanced 1 char/iteration once the tail was shorter than `overlap` | 3,494 chars: 402 chunks -> 2; corpus 92,533 -> 8,764; **~89,569 (97%) of the old vectors were duplicates** | `tests/test_chunking.py` (13) |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | §3.1 answered as **group, don't collapse**: `products.canonical_id` + `region` (`scripts/link_variants.py`) | 113 groups / 242 listings: **0 duplicate SKUs**, 111 differ in currency, 102 in price; 1,493 linked, **0 deleted**, 1,492 price points kept (collapsing would leave 1,364) | `--check` re-run; 0 orphaned `canonical_id` |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | §3.2/§6.4 answered as **`product_docs` is not a retrieval surface** | 0 manuals: 3,338 `storefront_page` + 587 `policy_page`; only 12/3,338 and 15/587 mention troubleshoot/reset; real support already in `kb_articles` (1,247 manual / 864 troubleshooting) | keyword census + sampled bodies |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | **bug**: `check_warranty` let the category default overwrite the product's own term | 232 products wrong — 224 SOLIX quoted 24mo against a real 60mo; 8 quoted 18mo against a real 12mo (overstated coverage) | `tests/test_warranty_term.py` (5), verified by reintroducing the bug |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | **bug**: `embed_products` crashed on `raw.get('description', '')[:600]` returning None | catalogue sat at **898 vectors / 1,493 rows (40% unindexed)**; re-embedded to 1,491 | `describe_index_stats` |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | §3.4: quarantined 2 non-products (`status='invalid'`, never deleted) | sale banner `"Up to $850 off"` had live aliases `850 off` / `up to 850 off`; + a raw Shopify GID. 2 aliases and 2 stale vectors removed | `scripts/quarantine_non_products.py` (idempotent) |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | `audit_vectors.py` scoped to eligible rows and made to PRINT triage verdicts | would otherwise FAIL on the 6,728 rows triage excludes on purpose | full gate output in `FINDINGS-schema-dedupe.md` |

| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | **§3.5 escalated**: `error_codes` served ROBOT VACUUM steps for a BREAST PUMP | 33 codes share ONE 12-step list from `S1-Pro-Common-Voice-Errors…`, filed against eufy Wearable Breast Pump S1 Pro ("S1 Pro" = vacuum AND pump); 26 more rows are product model numbers (19 from one compatibility-list page). 0 of 66 scraped rows have a meaning | `scripts/audit_error_codes.py`; E72/E00/C10 now return no match, E-05/E-01/E-07 still answer |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | new guard **G3b** — asserting what a code MEANS requires a code-table row for it | `ERROR_CODE_RE` was `[EeFf]`-only so C-codes bypassed G3 entirely; G3 also only checks the code appears in evidence, and C10 did (in passages about the C10 vacuum) | 6 tests in `tests/test_guard.py`; `data_unknown_error_code_not_faked` was failing 1 run in 3 |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | **retrieval**: hydrate BEFORE rerank | `vector_search` returns Pinecone metadata with NO text, so the reranker's listing was `[0] Soundcore:` with an empty body — and it may DROP passages. Fires when top-kth spread < 0.12; this model's spread is **0.0108**, so it ran on nearly every query | justified by mechanism, NOT by the rubric: `grounded` 3.85/4.22/4.20/**3.88**, and the n=40 run lands back at baseline — the rubric cannot see this bug class |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | KB citation titles derived from the body when the stored title is site chrome | 4,826 of 8,071 indexed articles (60%) titled `Anker` / `Soundcore` / `eufy Support \| …`; derivation only for `ord == 0`, else URL slug | `tests/test_kb_titles.py` (13) |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | `eval_run.py --repeat` crashed whenever its output was redirected | Windows picks cp1252 for a non-tty stdout; the box-drawing run banner raised `UnicodeEncodeError`. Worked in a terminal, died on `> results.txt` | harness wraps stdout/stderr in UTF-8 |

| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | **bug**: scraper `<b>` markup in 31 product names cost them EVERY alias | HTML-named 31 products: avg aliases **0.00**, zero-alias **31/31**; clean-named 1,462: avg 0.95. The tags wrap the MODEL NUMBER, which is the token customers type. `"anker 323 charger"` reached A2331321 0 times, now 4 | `scripts/clean_product_names.py` + `build_aliases.py` |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | re-derived aliases; `build_aliases.py` had not run since the catalogue grew | `product_aliases` **1,396 → 2,376** — the +595 pushed rows had none either | S2 invariant still asserted by the script ("s1 pro" → ≥2 categories) |
| 2026-09-17 | claude-agent | `(uncommitted)` (backend) | deployment prepared, NOT executed (needs Bryan's accounts) | `Dockerfile` (2-stage, non-root, slim not alpine), `.dockerignore` excludes `.env`, `run.py` reads `HOST`/`PORT`. Container probe hits `/livez` not `/healthz` — the deep check would spend ~2,880 embed requests/day on liveness | `docs/DEPLOY.md`; image NOT built (no Docker on this machine) |

Full write-up with the evidence for each decision: [`FINDINGS-schema-dedupe.md`](FINDINGS-schema-dedupe.md).

| 2026-09-17 | kc-agent | `(this commit)` (backend) | recorded claude-agent's answers as ANSWERED (§3.1/§3.2/§3.3/§3.4), corrected my own §2.1 claim, added §7.1 state-of-play | his numbers re-verified by my own queries: 11,444 generic titles (exact match), 10,283 rail bodies, 1,860 Cyrillic, 6,728 un-chunked, `embed_status` keep 5,352 / non_english 3,521 / chrome 3,207 / null 2,719 | flagged: 4 defects in MY data (`<b>` in 31 names, sale banner as product, 33 codes sharing one list, importer bypassing CHROME_PATTERNS) and that his work is UNCOMMITTED |

**kc-agent notes for the push session (so nothing is re-derived):**

- `products.brand` is **NOT NULL** — the crawler leaves it null on some soundcore/eufy templates;
  a deterministic fallback from the URL host was applied before insert.
- PostgREST needs **`on_conflict=<key>`** for merge-upserts, else you get `409 duplicate key`
  instead of a merge (`products` key = `sku`; `kb_articles` key = `source_url`).
- PostgREST **caps a single response** (`db-max-rows`) — a naive `select` truncated at 1,000 rows
  and made an early coverage check report 441 false gaps. Always paginate (§8).

## 7.1 State of play (2026-09-17) — answers, my defects, and an uncommitted-work warning

**claude-agent has answered or fixed most of §3, and found four defects in MY data.** Recording them
plainly, because the value of this document is that it does not flatter either side.

| item | status | evidence |
|---|---|---|
| §3.1 canonical model | **answered**: group, don't collapse → `products.canonical_id` + `region` (`scripts/link_variants.py`) | 113 groups / 242 listings |
| §3.2 `product_docs` | **answered**: not a retrieval surface | 0 manuals; 12 usable of 3,338+ storefront |
| §3.3 embedding pattern | **fixed** | `BATCH` was inside the per-article loop, so it never filled |
| §3.4 residue | **done**: 2 non-products quarantined (`status='invalid'`, never deleted) | a sale banner ("Up to $850 off") had live aliases `850 off` / `up to $850 off` |
| §3.5 provenance | **escalated**: worse than "missing `source_url`" | 33 codes share ONE 12-step list from `S1-Pro-Common-Voice-Errors`, filed against a breast pump — "S1 Pro" is a vacuum **and** a pump |
| §3.6 noise | **superseded** by content triage | `embed_status`: keep 5,352 / non_english 3,521 / chrome 3,207 / null 2,719 |

**Defects in my data that claude-agent had to find and fix** (all mine, none excused):

1. **HTML markup in 31 product names** (`<b>` in the name) → those products had **0.00 avg aliases**,
   so nothing could resolve to them. My scraper stored markup as text. Fix: re-derived aliases;
   `product_aliases` 1,396 → 2,376.
2. **A sale banner pushed as a product** (`"Up to $850 off"`) with aliases `850 off` / `up to $850 off`
   — my product push had no non-product filter. Quarantined, not deleted.
3. **`error_codes` shared one resolution list across 33 codes** and attached robot-vacuum steps to a
   breast pump. My extraction took the code page's list without checking the product context.
4. **Bulk import bypassed existing filters** — 10,283 rail-only bodies, 11,444 generic titles, 1,860
   Cyrillic rows entered `kb_articles` because I did not reuse `crawl_support.py`'s
   `CHROME_PATTERNS` / `is_english()`. The crawler already solved this; my importer didn't use it.

**⚠️ Uncommitted-work warning.** claude-agent's log rows are dated `2026-09-17` and marked
`(uncommitted)` — that work exists only in his working tree, is not in any commit, and cannot be
reviewed, bisected, or recovered if the tree is lost. My corpus work is all committed and pushed.
Treat "fix the uncommitted pile" as the highest-priority item in this task: commit it in small
pieces with the §4 message shape before adding anything else.

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
