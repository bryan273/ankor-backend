# Live Supabase inventory vs scraped goldset — what exists, what's missing

**Project:** `Anker-Hackathon` · ref `wmxucgywnafvlnybkjhi` · org `KC` · region AWS `ap-northeast-1` · Free plan
**URL:** `https://wmxucgywnafvlnybkjhi.supabase.co`
**Read on:** 2026-09-12, via KC's own signed-in dashboard (SQL editor over the Playwright extension bridge). **No keys used, nothing written.**

> ⚠️ The ref used in the shared keys doc (`wmxucgwnafvlnybkjhi`) is **wrong** — it is missing a character.
> The real ref is `wmxucg**y**wnafvlnybkjhi` (20 chars). Every earlier "project not found" came from this.

## 1. Exact row counts (all 25 public tables)

| Table | Rows | | Table | Rows |
|---|---:|---|---|---:|
| kb_chunks | 69,546 | | orders | 207 |
| kb_articles | 2,719 | | troubleshooting_flows | 84 |
| product_aliases | 1,396 | | guard_hits | 83 |
| product_media | 912 | | error_codes | 73 |
| products | 898 | | dealer_orders | 57 |
| tool_traces | 834 | | customers | 40 |
| messages | 821 | | dealers | 24 |
| message_blocks | 491 | | eval_cases | 22 |
| sessions | 406 | | warranty_policies | 5 |
| eval_runs | 260 | | attachments | 1 |
| ticket_events | 255 | | product_specs | **0** |
| tickets | 255 | | product_docs | **0** |
| order_items | 207 | | | |

## 2. Quality breakdown (the part that matters)

**products — 898**, 852 with price, **all `USD`**, 113 `BUNDLE-` SKUs
- brands: eufy 234 · Anker 226 · Anker SOLIX 223 · soundcore 215
- categories: power_station 223 · audio 173 · accessory 172 · charger 158 · security_camera 76 · power_bank 48 · service 13 · robot_vacuum 11 · cable 7 · smart_lock 6 · tracker 5 · breast_pump 3 · stick_vacuum 2 · **NULL 1**
- hero demo SKUs present: `T2080111` Robot Vacuum Omni **S1 Pro** · `T8D04121` Wearable Breast Pump **S1 Pro** · `T600P181` Breast Pump S2 Pro · `T8D02181` Breast Pump S1
- **bottle washer: 0 rows** ← the one demo-relevant product family with no coverage
- hygiene: 1 junk row (`eufy-up-to-850-off :: "Up to $850 off"`), 46 rows with no price

**kb_articles — 2,719** (2,719 distinct URLs, all `lang=en`, all with `product_ids`)
- hosts: **support.eufy.com 1,691 · support.soundcore.com 843 · support.anker.com 185**
- doc_type: faq 1,876 · troubleshooting 843
- URL shape: `https://support.<brand>.com/s/article/<slug>` (Salesforce Knowledge)

**kb_chunks — 69,546** · avg **38.9 chunks/article** · `chunks_embedded` 69,550 · `chunks_with_pinecone_id` 69,550
- ⚠️ **933 of 2,719 articles (34%) have no chunks at all** → never chunked, never embedded, so unreachable by vector search.
- ⚠️ 69,550 embedded vs 69,546 rows: 4-row drift (deleted chunks still in the index).

**error_codes — 73 rows** across 19 products, but only **10 distinct codes**: `E-01 E-02 E-03 E-05 E-07 E-08 E-13 E-21 F-02 F-06` — every row has `fix_steps`. Coverage is thin and synthetic-looking.

**warranty_policies — 5 rows**, one per category (robot_vacuum 12m · breast_pump 12m · charger 18m · power_station 24m · audio 18m), each with structured `covers`/`excludes`.

**dealers — 24 rows / 22 authorized / only 12 distinct names**, regions AU·DE·ID·JP·MY·SG·UK·US·**XX**, no de-dup (`Grey Market Imports/XX` appears twice).

**product_aliases — 1,396 rows / 1,022 distinct** (`s1 pro` appears twice → alias resolution can hit duplicates).

**Empty:** `product_specs` 0, `product_docs` 0.

## 3. Scraped goldset vs live — overlap measured, not guessed

| goldset table | rows | live equivalent | verdict |
|---|---:|---|---|
| products | 741 | 898 | live is bigger; only the **bottle washer** line is genuinely missing live |
| support_articles | 1,496 | kb_articles 2,719 | **partial overlap only** |
| error_codes | 66 (**66 distinct**) | 73 rows / **10 distinct** | **strong supplement** |
| policies | 137 (but 1 category) | 5 (5 well-formed categories) | live is better structured — skip |
| dealers | 34 (channels: brand-page/other) | 24 | marginal — but live needs the de-dup |

**KB overlap, measured:** 40 slugs sampled from my 1,496 → **13 matched** live by normalized slug (**≈32%**, so ≈2/3 of my slugs are not in their table). Caveat: their slugs are URL-encoded in places (`Guia-do-usu%C3%A1rio`) and mine are normalized, so 32% is a **floor**; the true overlap is somewhat higher. Both sides also hold unique content — I never scraped soundcore (their 843), and my eufy crawl (1,006) is smaller than theirs (1,691).

## 4. Proposed supplement (nothing pushed — Bryan is mid-build on the shared project)

1. **error_codes** — add my 66 distinct codes (with `fix_steps`, `meaning`, `source_url`) mapped to `products.id` by SKU. Their 10 distinct codes → ~72. No embeddings needed.
2. **kb_articles delta** — import only slugs absent live (≈1,000), then chunk + embed. ⚠️ Blocked on an **embedding key** (Gemini/OpenAI direct — RKAPI cannot embed), and it would also close the **933-article embedding gap** using their own rows.
3. **bottle-washer products** — add the eufy Bottle Washer S1 Pro SKUs so the demo's second product family resolves.
4. **Hygiene** (independent of my data): de-dup `dealers` (24→12 distinct) and `product_aliases` (1,396→1,022), delete the promo row in `products`, and decide the `region='XX'` grey-market rows.

## 5. Reusable access recipe (no keys, read-only)

```
1. playwright-ext MCP (extension bridge) must be attached — token lives in the plugin env.
2. Navigate: https://supabase.com/dashboard/project/wmxucgywnafvlnybkjhi/sql/new
3. Set SQL:   window.monaco.editor.getModels()[0].setValue(sql)
4. Run:       document.querySelector('[data-testid="sql-run-button"]').click()   // JS click; normal click can hang on visibility checks
5. Read:      the result grid is canvas-rendered → NOT in the DOM.
              Read scalars via the error channel instead:
              do $$ begin raise exception 'PROBE %=%', k, v; end $$;
              The message renders as text: "Failed to run sql query: ERROR: P0001: PROBE ..."
```
