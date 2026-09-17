# Findings — TASK-schema-dedupe

Reply to `TASK-schema-dedupe.md`. Every number here was measured against the live
Supabase catalogue on 2026-09-17; the scripts that produced them are named so they can be
re-run.

Three of the task's prescriptions turned out to rest on premises the data contradicts.
They are called out individually below rather than quietly skipped, because in each case
following the instruction would have made the agent worse.

---

## §6.1 / §6.2 — canonical selection, and whether regional variants stay queryable

**Answer: they stay. Nothing is collapsed, nothing is deleted.** `products` gains
`canonical_id` (self-referential) and `region`. Applied by `scripts/link_variants.py`
(idempotent, `--check` for a dry run).

§3.1 describes the 113 slug groups as duplicates and prescribes collapsing each to one
canonical row. The catalogue disagrees:

| measure | value |
|---|---|
| duplicate SKUs in `products` | **0** |
| slug groups with >1 listing | 113 (242 rows) |
| groups differing in **currency** | **111** |
| groups differing in **price** | **102** |

```
a1641    A1641011 =  59.99 USD   anker.com/products/a1641
         A1641H21 = 129.95 AUD   anker.com/au/products/a1641

a2331    A2331111 =  19.99 USD   global
         A2331T21 =  39.99 AUD   au
         A2331321 =  27.99 EUR   eu-en
```

These are one model sold in three storefronts at three prices. Collapsing them deletes
SKUs that appear on real customer orders and leaves one arbitrary currency behind, so the
agent quotes USD to an Australian customer. §3.1's own gate — *"every SKU that existed
pre-migration still resolves"* — would have reported PASS while the answer about money
became wrong.

Two further facts make the alias route unworkable as specified:

- **SKU resolution does not go through `product_aliases` today.** All 1,493 SKUs resolve
  directly against `products.sku`; 0 product SKUs appear in the alias table, which holds
  *names* (`"s1 pro"`). Alias-ification would not preserve what deletion removed.
- `product_aliases` has columns `alias, product_id, confidence, source` — it cannot carry
  a price or a currency, so the regional facts have nowhere to go.

Grouping instead of collapsing delivers what §3.1 actually wanted — three near-identical
listings stop competing for the same top-k — while each row keeps its SKU, price,
currency and URL.

**Canonical rule** (deterministic, in order): not a bundle, then global storefront over a
regional one, then USD, then lowest SKU. This is §6.1's "global-URL row", with the
non-bundle test first because a bundle is a different purchase rather than a translation
of one.

**Result:** 1,493 rows linked · 0 orphaned `canonical_id` · **0 rows deleted** ·
**1,492 distinct price points preserved** (collapsing would have left 1,364).

## §6.3 — are `BUNDLE-*` / `COMBO-*` products?

**Products.** 339 `BUNDLE-*` and 4 `COMBO-*` rows have their own SKU, own price and own
warranty, and a customer who bought one holds exactly that SKU. They are never chosen as
a family's canonical row, so they no longer displace the base product in search.

## §6.4 — is `product_docs` a retrieval surface?

**No, and it should not become one.** §3.2 describes these rows as *"manual/PDF parses —
exactly the material for fault-localisation"*. There is not one manual among them:

| `kind` | rows | avg chars |
|---|---|---|
| `storefront_page` | 3,338 | 2,820 |
| `policy_page` | 587 | 4,588 |

`schema.sql` documents `kind` as `manual | quickstart | datasheet | faq`; the loaded data
matches none of those values. Sampled bodies are product listings and sale banners
("Vuelta al cole: Ahorra hasta 45%", "Ausverkauft", brand nav rails). `policy_page` is
mislabelled too — its most frequent titles are "eufy Security Prime Sale | Save on
Cameras", "Exclusive Discounts", "Anker | Live Charged."

Keyword census:

| | rows | mentions "troubleshoot"/"factory reset" |
|---|---|---|
| `storefront_page` | 3,338 | **12** |
| `policy_page` | 587 | **15** |

1,817 of 3,338 storefront pages mention "warrant", but that is the footer badge on every
product page, not policy text.

Running the prose gate over them returns "keep" for 2,584 — which is the gate working as
designed and still being the wrong question. `triage_kb_corpus.py` measures whether text
*is prose*; marketing copy is excellent prose. Admitting it would put sale copy into the
same top-k as repair steps, which is the failure the task doc itself warns about.

The support material is already in `kb_articles`: **1,247 `manual` · 864
`troubleshooting` · 2,526 `faq` · 134 `policy`** (the real policy rows average 9,218
chars against `policy_page`'s 4,588). Product marketing already has a home too —
`product_text()` embeds name, price, category and specs per product into the `products`
namespace.

**Recommendation for KC:** `product_docs` is storage/provenance, not a retrieval surface.
Drop it from the push set, or keep it as a crawl archive.

## §3.6 — prune `product_docs` noise: moot

§2.2 orders this before §3.3 because "quota is spent at embed time". It is not: nothing in
`product_docs` is ever embedded, before or after the pruning, because it is not a retrieval
surface (§6.4). The table costs no quota at all, so pruning it cannot save any.

The 506 rows are also not the interesting noise. The 3,419 rows *over* 500 chars are the
same marketing copy at greater length, and length was never the signal that separated
support text from storefront text — that was the finding that produced the prose gate in
the first place.

## §3.2 — `dealers` and `warranty_policies`

- **`dealers`**: keep KC's 56 where-to-buy records out. Ours are 24 curated service-routing
  rows with `order_no_pattern` — the agent uses them to recognise a dealer invoice and
  route a claim. A where-to-buy link cannot answer that question, and mixing the two makes
  `dealer_matched` mean two different things inside the warranty engine.
- **`warranty_policies`**: confirmed as category-level defaults, and they are now only a
  *fallback* — see bug 1 below.

## §3.4 — backfill residue

16 NULL `category`, 46 NULL `price`, 62 NULL `warranty_months` (47 of them `accessory`,
a category with no policy row, so they fall through to the global default).

Two of the 16 are not products at all:

```
eufy-up-to-850-off                            "Up to $850 off"   (a sale banner)
gid://shopify/ProductVariant/53422734901615   "100"              (a raw Shopify GID)
```

The banner carried **live alias rows** — `850 off` and `up to 850 off` — so
`find_by_alias` could return it and the agent could offer a customer a product called
"Up to $850 off". Its URL points at `t2070111`, a real product: the scraper read the sale
banner as the product name.

`scripts/quarantine_non_products.py` marks both `status='invalid'`, deletes the 2 aliases
pointing at them, and removes their 2 stale Pinecone vectors. **Neither row is deleted** —
the task doc's rule is read here as "a scrape artifact is evidence about the crawler worth
keeping". `status='invalid'` is now excluded from `find_by_alias`, the category picker,
and `embed_products`.

---

## Bugs found while measuring

### 1. The agent quoted the wrong warranty term for 232 products

`check_warranty` read the product's own `warranty_months`, then let the category default
**overwrite** it — the specific fact lost to the generic one.

```
power_station   product=60mo   policy=24mo   x224
charger         product=12mo   policy=18mo   x7
audio           product=12mo   policy=18mo   x1
```

Every Anker SOLIX power station carries 60 months and the agent said 24. Eight products
go the other way — quoting 18 months on a 12-month product promises half a year of
coverage that does not exist, which is the direction that costs money.

Fixed in `app/agent/tools.py`. The category row remains the fallback for the 62 products
with no term of their own, and stays the only source of `covers`/`excludes`. Pinned by
`tests/test_warranty_term.py` (5 tests, verified by reintroducing the bug: 3 fail, then
pass).

### 2. 40% of the catalogue was invisible to search

`products` held **898 vectors against 1,493 rows**. Not a stale run — a crash:

```python
raw.get("description", "")[:600]     # returns None when the key EXISTS with a null value
TypeError: 'NoneType' object is not subscriptable
```

`embed_products` died mid-run at ~898 rows. Nothing checked the exit code, and because kb
ran separately every other namespace looked healthy. Fixed, along with the same pattern in
`app/agent/graph.py` and `app/agent/tools.py`. Products re-embedded: **1,491 vectors**
(1,493 minus the 2 quarantined).

### 3. The KB index was 97% duplicates (§3.3)

`chunk_text` never terminated cleanly. Once `end` reached the end of the text,
`end - overlap` fell *behind* `start`, so the `max(..., start + 1)` guard advanced **one
character per iteration**, emitting a near-identical chunk until the text ran out.

```
19,726 chars: was 408 chunks -> now 8
 8,544 chars: was 404 chunks -> now 4
 3,494 chars: was 402 chunks -> now 2
```

2,719 articles held 92,533 chunks where ~2,964 were correct — **~89,569 duplicate
vectors**. `kb.MAX_PER_ARTICLE` was invented to stop one article swamping every top-k; it
was this bug wearing a disguise.

Pinned by `tests/test_chunking.py` (13 tests).

### 4. The batch size never applied to the KB (§3.3, second half)

§3.3 was implemented as `batchEmbedContents` with `BATCH=100` — but the flush sat *inside*
the per-article loop, over one article's chunks. Articles average under two chunks, so the
window never filled: **one embed request, one Pinecone upsert and one DB round-trip per
article.** Measured at 36 articles/min — 3.7 hours and ~8,000 requests for this corpus,
which is the request-per-chunk pattern that already cost this project a Google account, in
a different disguise.

Chunks now queue across articles. **23× faster**; the whole corpus rebuilds in ~7 minutes.

### 5. Scraper markup in 31 product names cost them ALL their aliases

`products.name` carried `<b>` tags — search-result highlighting the crawler copied whole:

```
'Anker <b>323</b> Charger (33W)'
'Anker <b>575</b> USB-C Docking Station (13-in-1)'
```

Easy to file as cosmetic. It is not:

| | products | avg aliases | with ZERO aliases |
|---|---|---|---|
| HTML-named | 31 | **0.00** | **31 of 31** |
| clean-named | 1,462 | 0.95 | 873 |

`build_aliases.py` derives aliases from the name, and the tags wrap **the model number** —
the one token a customer actually types. So `"anker 323 charger"` could not reach
`A2331321` through the alias path at all, which is the deterministic half of
disambiguation. After `scripts/clean_product_names.py` it returns 4 matches.

Re-deriving also revealed a second gap: `product_aliases` went **1,396 → 2,376**, because
the script had not been run since the catalogue grew 898 → 1,493. The 595 pushed rows had
no aliases either.

(The 873 clean-named products with no aliases are a milder, separate matter — coverage is
sparse by design, derived only where a short name is inferable. 31 of 31 is categorical,
and that is what made it worth chasing.)

### 6. `audit_vectors.py` would have failed for the right work

Its second direction counted every article with a body as needing an index, so the 6,728
rows `triage_kb_corpus.py` deliberately excludes would have reported as a FAIL. It now
scopes to eligible rows and **prints the triage verdicts** rather than filtering them out
silently — an audit that quietly drops rows from its own denominator is exactly how 81% of
the corpus once went missing while the script reported PASS.

---

## Gate results

```
Pinecone:  kb 8,764 · products 1,491 · tickets 220 · dealers 24
Postgres:  14,799 articles · 8,764 chunks · 8,764 distinct pinecone_ids

kb vectors:            8764
backed by a chunk row: 8764
orphaned:                 0  (0%)

triage verdicts:  keep 8,071 · non_english 3,521 · chrome 3,207
eligible articles:     8071
reached the index:     8071
never embedded:           0  (0%)

PASS: every kb vector is backed by a chunk row, and every article reached the index.
```

§3.1 gate: 0 orphaned SKUs, 0 rows deleted, 1,492 of 1,492 price points preserved.

## §3.5 revisited — the error-code table is worse than "missing attributions"

§3.5 asks for `source_url` to be filled in for 73 rows. Chasing a hallucination in the
eval turned up a much larger problem in the same table. Of 139 rows, 73 are the demo seed
and 66 were scraped, and **not one scraped row has a meaning**. They are two distinct
extraction bugs:

**26 rows are product model numbers.** Nineteen come from one page,
`Eufy-Smart-Display-E10-T87A0-Compatibility-List` — a table of compatible eufy *models*.
The rest trace to product manuals (`Robot-Vacuum-Auto-Empty-C10-…`, `EufyCam-C37-…`, a
USB-cable FAQ). `C10` is a vacuum, not a fault. These already answer nothing, but they
made the table claim 139 codes when it holds at most 80.

**33 rows carry one article's steps, stamped onto every code it mentions.** Thirty point
at `S1-Pro-Common-Voice-Errors-and-Basic-Troubleshooting-Guide` and share an identical
12-step list that is visibly several remedies concatenated — dustbin (1-3), main brush
(4-8), side brush (9-11), water tank (12).

What makes this the most serious finding in this document is *what they are attached to*:
**the eufy Wearable Breast Pump S1 Pro.** "S1 Pro" names both the Robot Vacuum Omni S1 Pro
and the Wearable Breast Pump S1 Pro — the ambiguity this project's S2 scenario is built
on — and the extractor resolved it to the pump, then attached the vacuum's guide.

So a mother asking about error E72 on her breast pump was told to *"turn the robot over"*
and *"clean the garbage on the brush and in the brush slot"* — served by
`lookup_error_code`, the deterministic tool the agent is instructed to trust above RAG.
`kb.py`'s own docstring calls a robot-vacuum instruction in a breast-pump thread "the
worst failure this system can make".

Re-pointing them at the vacuum would not fix it: all 33 would still share one step list,
so 33 different faults would get one identical answer. `scripts/audit_error_codes.py`
marks them `scraped_unattributed` (and the 26 `scraped_model_number`), nothing is deleted,
and `find_error_code` no longer serves either class. Verified: `E72`, `E00`, `EB1K`, `C10`
now return no match; `E-05`, `E-01`, `E-07` still answer correctly.

**For KC:** both are extractor bugs worth fixing upstream — a compatibility table read as
a code table, and an article's body attached to every code it names.

### The guard that should have caught it

`data_unknown_error_code_not_faked` failed one run in three: the agent stated what C10
means. Two holes, both now closed:

- `ERROR_CODE_RE` was `[EeFf][-–]?\d{1,3}` — **C-prefixed codes were never checked at
  all**, so G3 never looked at C10.
- G3 only asks whether the code appears *somewhere* in the evidence. C10 did — in KB
  passages about the C10 vacuum. The code was grounded while its meaning was invented.

New rule **G3b**: asserting what a code means requires `lookup_error_code` to have
returned a row with a meaning or steps *for that code*. A manual that merely mentions the
code is not a definition. Six tests in `tests/test_guard.py`.

## What the eval could and could not show

Four runs, 135 cases in total:

| run | cases | grounded | judged | state |
|---|---|---|---|---|
| 1 | 27 | 3.85 | 20/20 | before the retrieval fix |
| 2 | 27 | 4.22 | 18/20 | hydrate-before-rerank |
| 3 | 27 | 4.20 | 20/20 | same |
| 4 | 54 | **3.88** | **40/40** | + G3b + error-code provenance |

Runs 2 and 3 looked like a +0.35 lift in `grounded`, and it does not survive run 4, which
has twice the judged sample and lands back at run 1. With n=20 and an LLM judge, that
spread is noise.

**So the retrieval fix is justified by mechanism, not by this rubric.** The reranker was
provably being handed empty strings and is permitted to drop passages; that is a defect
whether or not a 0-5 prose score moves. The honest conclusion is the other way round:
**the rubric cannot detect this class of bug at all**, which is worth knowing before
trusting it as a regression gate. The same is true of the structural checks — the suite
was 27/27 green while the reranker ranked blanks, 60% of citations were labelled
"Soundcore", and `lookup_error_code` served vacuum steps for a breast pump.

Every defect in this document was found by measuring the data, not by the eval going red.
The one exception, `data_unknown_error_code_not_faked`, only failed one run in three.

## §3.5 — still declined, with the same evidence

The gate `source_url is null → 0` is reachable only by fabricating attributions. **0 of 12
sampled codes appear in any article body**; the 73 rows are the demo seed in
`scripts/seed_demo.py` (`E-01` "Left wheel jammed", `E-05` "Brush roll blocked"). Marked
`provenance = demo_seed` (73) / `scraped` (66) instead.
