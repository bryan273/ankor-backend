# Implementation Plan — Anker Care Agent

> Phased build for both repos. Every phase ends with something demonstrable; nothing is "done" until its acceptance checks pass.
> Read [`SPECIFICATION.md`](SPECIFICATION.md) first for the *what*, [`API_CONTRACT.md`](API_CONTRACT.md) for the *wire*.
> Estimates assume one person per repo working in parallel. Total ≈ 6.5 days.

---

## Phase 0 — Scaffold and preflight · 0.5 day

**Backend**

```
anker-hackathon-backend/
├─ app/
│  ├─ main.py                 FastAPI app, CORS for :3000, X-API-Key dep
│  ├─ config.py               pydantic-settings
│  ├─ sse.py                  event emitter + the v1 vocabulary as an enum
│  ├─ schemas/                contract models (events, blocks, tool IO)
│  ├─ clients/                rkapi.py (lane pool), embed.py, pinecone.py, supabase.py
│  ├─ agent/                  graph.py, nodes/, tools/, prompts/, guard.py, policy.py
│  ├─ services/               products.py, kb.py, orders.py, warranty.py, tickets.py
│  ├─ routes/                 chat.py, attachments.py, catalog.py, tickets.py, eval.py
│  └─ db/                     models.py, migrations/ (alembic)
├─ scripts/                   crawl_*.py, embed_corpus.py, seed_demo.py, gen_ts_types.py, eval_run.py
├─ tests/
├─ data/                      crawl cache + parsed corpus (gitignored)
└─ docs/                      these files
```

Tasks:
1. `python -m venv .venv` · `pip install fastapi uvicorn[standard] sse-starlette langgraph langchain-core openai google-genai pinecone supabase pydantic-settings httpx selectolax pymupdf tenacity structlog pytest pytest-asyncio alembic psycopg[binary]`
2. `GET /healthz` pings every dependency and reports per-dep status.
3. `POST /api/v1/chat` echo stream emitting the full vocabulary with fake data — lets the frontend build against a real stream on day 0.
4. `clients/rkapi.py`: openai SDK with `base_url`, **explicit `User-Agent`**, N-key `asyncio.Semaphore` lane pool, retry on 429/5xx with jitter.

**Frontend**

`create-next-app` (TS, App Router, Tailwind) + shadcn/ui + `@supabase/ssr` per the snippet in the brief. `useEventStream` hook against the echo endpoint. Block registry with a fallback renderer.

**Acceptance**: `/healthz` green except Supabase-write; the frontend renders a fake diagnostic-steps block streamed from the echo endpoint.

---

## Phase 1 — Data pipeline · 1 day

1. **Crawl** (`scripts/crawl_products.py`): pull `server-sitemap-index-products.xml` from `anker.com`, `eufy.com`, `soundcore.com`, `ankersolix.com`, `ankerwork.com`; filter to product URLs; fetch at 1–2 rps with an on-disk cache; extract JSON-LD `Product` first, DOM spec-table fallback. Target ≈ 400–800 products.
2. **Support corpus** (`scripts/crawl_support.py`): follow manual/FAQ links off product pages into `support.anker.com`; download PDFs; PyMuPDF → text; extract error-code tables into `error_codes`.
3. **Media**: hero + gallery download; VLM caption each into `product_media.caption` + `vlm_tags`.
4. **Aliases** (`scripts/build_aliases.py`): generate `product_aliases` from names — **assert that `"s1 pro"` maps to ≥2 products across brands**, since that ambiguity is scenario S2 and its absence means the crawl missed eufy Baby.
5. **Seed** (`scripts/seed_demo.py`): 12 dealers with distinct order-number patterns, ~120 orders, ~200 historical tickets, ~40 customers. Includes the S3 fixture: an order number present only in `dealer_orders`.
6. **Embed** (`scripts/embed_corpus.py`): chunk → `gemini-embedding-001` (`RETRIEVAL_DOCUMENT`) → Pinecone upsert into `products` / `kb` / `tickets` / `dealers` / `community`. Content-hash skip so re-runs are near-free.

**Acceptance**: `search_kb("robot vacuum reduced suction")` returns manual chunks with correct SKU metadata; the alias assertion passes; `lookup_order` on the S3 fixture returns `not_found` while `lookup_dealer_order` finds it.

---

## Phase 2 — Agent core · 1.5 day

1. Nodes `ingest → rewrite → perceive → disambiguate → plan/act → guard → compose → persist`.
2. Postgres checkpointer on Supabase so `interrupt`/resume survives a reload.
3. All 11 tools with typed Pydantic IO and a compact JSON view for the model.
4. Rule engine `services/warranty.py` — pure functions, table-driven, no LLM.
5. Guard rules 1–6 with rule ids logged to `guard_hits`; a violation triggers one repair re-plan, then `GUARD_BLOCKED`.
6. Prompts in `agent/prompts/` — kept flexible, not rigid templates: describe the goal and the constraints, let the model phrase it.
7. Stream every node.

**Acceptance**: S1–S4 pass end to end via `curl`; every `stage_start` closes; `check_warranty` is called before any coverage sentence in 20/20 runs.

---

## Phase 3 — Dynamic UI · 1.5 day

**Backend**: block emitters for all 13 types; `/chat/action` resume path; `BLOCK_STALE` handling; `gen_ts_types.py` emitting `contract.ts`.

**Frontend**:
- Split layout: chat left, workbench right; short answers stay inline, long artefacts move right with a one-line chat summary.
- Components per block type; each owns its action posting and optimistic `answered` state.
- Stage pills with friendly labels; `thinking_delta` under a collapsed "what I'm doing" line.
- Citation chips with hover cards.
- Photo drop / clipboard paste / file picker → `/attachments`, thumbnail immediately, caption echo when it lands.
- Trace drawer (dev + judges): stages, tool calls with timings, guard hits, token usage.
- `/products` catalog page, `/tickets/[id]`, `/admin/eval`.
- EN + 中文 i18n; the agent replies in the user's language regardless of UI locale.

**Acceptance**: S2 renders a `product_picker`; selecting an option resumes the same turn and the answer continues without re-asking anything.

---

## Phase 4 — Multimodal, emotion, escalation · 1 day

1. Attachment VLM pass with the structured `vlm_facts` schema; facts merged **before** routing.
2. Emotion → policy table wired into `compose`; `emotion` event drives a subtle UI tone shift.
3. Safety triggers (swelling battery, smoke, burning smell, water ingress) short-circuit to `escalate(urgent)` with a safety block.
4. Ticket lifecycle + timeline + `human_handoff` block.
5. Deadline handling: a detected deadline produces a time-boxed plan and a plan B.

**Acceptance**: S1 with a photo produces — acknowledgement first, error code read off the image, ≤3 steps, deadline named; a "battery is swelling" message never enters troubleshooting.

---

## Phase 5 — Eval, load, polish · 1 day

1. 50–60 `eval_cases`; `scripts/eval_run.py` writes `eval_runs`; `/admin/eval` renders pass rate by scenario with transcript drill-down.
2. k6 script, 50 VUs replaying the golden set: assert p95 first-token < 4 s, full-answer < 25 s, zero unclosed stages, zero 5xx.
3. Tune the lane pool and caches against the load results.
4. Demo script: a 5-minute run through S1 → S2 → S3 → escalation, with the trace drawer open at the right moment.
5. README with a one-command local start.

**Acceptance**: ≥ 90 % deterministic assertions pass; judged empathy ≥ 4/5 mean; load targets met.

---

## Task board

| # | Task | Repo | Phase | Depends on |
|---|---|---|---|---|
| 1 | FastAPI scaffold + healthz | BE | 0 | — |
| 2 | RKAPI lane-pool client (UA header!) | BE | 0 | 1 |
| 3 | SSE emitter + vocabulary enum | BE | 0 | 1 |
| 4 | Echo `/chat` stream | BE | 0 | 3 |
| 5 | Next.js scaffold + `useEventStream` + block registry | FE | 0 | 4 |
| 6 | Supabase migrations (all tables) | BE | 1 | **service_role key** |
| 7 | Pinecone index create (3072, cosine) | BE | 1 | — |
| 8 | Product crawler | BE | 1 | — |
| 9 | Support/manual crawler + PDF parse + error codes | BE | 1 | 8 |
| 10 | Media download + VLM captions | BE | 1 | 8, 2 |
| 11 | Alias builder + S1 Pro assertion | BE | 1 | 8 |
| 12 | Demo seeder (dealers/orders/tickets) | BE | 1 | 6 |
| 13 | Embed + upsert pipeline | BE | 1 | 6, 7, 9 |
| 14 | LangGraph skeleton + checkpointer | BE | 2 | 6 |
| 15 | `perceive` structured call | BE | 2 | 14 |
| 16 | Disambiguation node + interrupt | BE | 2 | 11, 14 |
| 17 | Tool registry (11 tools) | BE | 2 | 13 |
| 18 | Warranty rule engine + tests | BE | 2 | 12 |
| 19 | Guard rules + repair loop | BE | 2 | 17 |
| 20 | Composer (empathy, citations, suggestions) | BE | 2 | 19 |
| 21 | Block emitters (13 types) | BE | 3 | 20 |
| 22 | `/chat/action` resume | BE | 3 | 14, 21 |
| 23 | TS type generator | BE | 3 | 21 |
| 24 | Chat + workbench layout | FE | 3 | 5 |
| 25 | Block components | FE | 3 | 23, 24 |
| 26 | Attachment upload + caption echo | FE | 3 | 27 |
| 27 | `/attachments` + VLM facts | BE | 4 | 2 |
| 28 | Emotion policy + `emotion` event | BE | 4 | 15 |
| 29 | Safety triggers | BE | 4 | 19 |
| 30 | Tickets + handoff | BE | 4 | 21 |
| 31 | Trace drawer | FE | 4 | 24 |
| 32 | i18n EN/中文 | FE | 4 | 24 |
| 33 | Eval harness + cases | BE | 5 | 20 |
| 34 | `/admin/eval` | FE | 5 | 33 |
| 35 | k6 load test + tuning | BE | 5 | 33 |
| 36 | Demo script + READMEs | both | 5 | all |

---

## Definition of done, per scenario

**S1 — angry + photo + deadline**
Acknowledges the frustration in the first sentence · names the deadline · reads `E-05` off the image · ≤3 steps before checking in · escalates after one failed step · never opens with a question.

**S2 — "S1 Pro isn't sucking"**
Never answers before resolving the product · uses purchase history when available and says so · otherwise shows a `product_picker` with photos · resumes without re-asking anything already said.

**S3 — order not found**
Distinguishes `not_found` from `error` · recognises the dealer number format · matches the dealer directory · warranty verdict comes from the rule engine · states exactly what evidence is needed and what happens next.

**S4 — vague / emotional**
Chooses reassure vs guide vs escalate deliberately, and the trace shows why · always ends with a concrete next step and 2–3 suggestions · a ticket exists whenever the loop could not close.

---

## Risks to watch during the build

- **Crawl thinness.** If product pages yield little text, the manuals carry retrieval. Check after task 9, not at the end.
- **Latency of a reasoning model.** Stream stages early; if p95 slips, run retrieval before the ReAct loop rather than inside it.
- **Checkpoint bloat.** Trim tool results in state to summaries; keep full results in `tool_traces`.
- **Contract drift.** Regenerate `contract.ts` in the same commit as any schema change; CI diff-checks it.
