# Anker Care Agent — Backend

> Anker 首届黑客松挑战赛 · 赛道 04 智能服务（真正听懂，真正解决）
> **Track 4 — Intelligent Services.** An after-sales support agent that reads the person, not just the sentence.

FastAPI + a hand-rolled ReAct loop over `gpt-5.6-terra`, grounded in a real crawl of Anker's product catalog and support knowledge base. Frontend lives in [`anker-hackathon-frontend`](https://github.com/tkc88888888/anker-hackathon-frontend).

---

## What it does

The brief names four scenarios. They are the specification, and the eval suite asserts each one end to end:

| | Scenario | What happens |
|---|---|---|
| **S1** | Angry customer photographs an error before a party | Reads the emotion *and* the deadline, acknowledges before troubleshooting, OCRs `E-05` off the photo, gives a time-boxed plan |
| **S2** | *"My S1 Pro isn't sucking anymore"* | Recognises that **S1 Pro names two different products** — a robot vacuum and a wearable breast pump — and asks which, before answering |
| **S3** | Order number returns nothing | Recognises a dealer invoice by its format, finds the dealer, and runs the warranty **rule engine** to decide what happens next |
| **S4** | Vague or unsafe | Judges between reassure · guide · escalate; a swelling battery skips diagnosis entirely and opens an urgent ticket |

Two of those are deliberately **not** LLM decisions:

- **Warranty eligibility** is a pure function over a table ([`services/warranty.py`](app/services/warranty.py)). An LLM that "decides" warranty will eventually promise a refund the company owes nobody, fluently. Guard rule **G1** refuses to let a coverage sentence through unless the engine actually ran.
- **Product disambiguation** starts with an alias table, not a vector search. `"s1 pro"` maps to two SKUs in two categories, and that fact is asserted by [`build_aliases.py`](scripts/build_aliases.py) — if a crawl ever loses eufy Baby, the build fails loudly instead of the demo quietly becoming an ordinary lookup.

---

## Current state

```
123 unit tests                      pass
21/22 scenario evals                pass   (S1-S4 plus everyday support and adversarial)
rubric (LLM judge, 0-5)             empathy 4.35 · proactivity 4.35 · clarity 4.06
50 concurrent users                 50/50 clean, no load shed
```

The eval corpus is 22 cases, not 4. The brief names four scenarios, but an agent that
only handles four is a demo — so it also covers the customer who wants a refund rather
than a repair, the one asking about compatibility before buying, the one who already
tried everything, the one writing Chinese, and the one trying to talk it into a free
replacement.

**Models are routed by capability.** `deepseek-chat` handles every text call and
`gpt-5.6-terra` handles vision, because DeepSeek has none. Measured on this workload:
perception 0.9 s vs 3.2 s, composition 1.4 s vs 2.8 s, with longer and warmer answers.
`TEXT_PROVIDER=rkapi` puts everything back on one model.

**Latency** — single turn p50 ~11-15 s. Under 50 concurrent users: p50 32 s, p90 66 s, and
the first stage pill lands in **0.9 s**, so the interface never looks dead while the model
works. Cost holds at ~0.019 credits per turn under load.

Getting there took one real fix. The client originally allowed a single in-flight request
per key, inherited from a heavy document-parse workload where requests genuinely
serialise. Measuring this workload showed one key handling 8 concurrent small calls at
2.16 calls/s with no errors — so the limit was throttling throughput roughly eightfold,
and at 50 users the median wait was **377 s**. Raising it to 8 per key took p50 to 32 s,
a 12x improvement, with every user served.

Live data, all crawled or seeded into Supabase:

| | |
|---|---|
| products | 898 across 4 brands, 99% with photos, 72% with prices |
| aliases | 1,396 pairs — the deterministic half of disambiguation |
| KB articles | 531 from `support.anker.com` / `support.eufy.com`, chunked into 31,406 passages |
| vectors | **32,498** in Pinecone — 31,406 `kb` · 860 `products` · 220 `tickets` · 12 `dealers`, 3072-d cosine |
| demo commerce | 40 customers · 207 orders · 12 dealers · 27 dealer orders · 220 resolved tickets (32 distinct resolutions) |
| troubleshooting | 42 curated flows · 73 error codes |

---

## Running it

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env          # fill in the keys
python run.py                 # http://127.0.0.1:8000
```

Use `run.py`, not `uvicorn app.main:app`. On Windows, psycopg's async driver refuses to run on the default `ProactorEventLoop`, and `uvicorn.run()` resets the event-loop policy to the platform default *after* import — so setting a policy at import time is silently undone. `run.py` keeps the loop ours. The failure it prevents is not an exception; the pool just never opens and every query reports a closed pool thirty seconds later.

### Rebuilding the corpus from scratch

```bash
python scripts/db_bootstrap.py --apply     # schema (idempotent)
python scripts/crawl_products.py --limit 220
python scripts/crawl_support.py --limit 550
python scripts/seed_legacy_products.py     # discontinued but still supported — this is what makes S2 exist
python scripts/build_aliases.py            # asserts the "s1 pro" ambiguity
python scripts/seed_demo.py                # asserts the S3 dealer fixture
python scripts/embed_corpus.py             # content-hash skip: a re-run is near-free
```

### Testing

```bash
pytest -q                                  # 123 unit tests, no network
python scripts/eval_run.py                 # 22 scenario evals against a running server
python scripts/eval_run.py --scenario S2
python scripts/load_test.py --users 50     # concurrency
```

---

## How a turn works

```
ingest → (rewrite ∥ perceive) → disambiguate → plan/act ⟳ → answer → guard → persist
```

- **ingest** — attachment facts, already computed at upload time so the turn doesn't pay for vision twice.
- **safety shortcut** — runs on the raw text *before* any model call. A burning smell should not wait for a ReAct loop.
- **rewrite ∥ perceive** — independent, so they run concurrently. `perceive` is one call returning emotion, intensity, urgency, intent, entities and language together, because those fields are mutually informative and three classifiers would triple the latency for no accuracy.
- **disambiguate** — alias table → purchase history → symptom vocabulary → photo → *then* ask. Silent resolutions state their reasoning in one clause, so a wrong guess is cheap to correct.
- **plan/act** — ReAct over 11 typed tools, capped at 6 iterations.
- **answer** — streamed live, optimistically.
- **guard** — six rules on the finished draft. When one fires, `content_reset` retracts what was shown and the corrected answer is written in its place. Most drafts pass, so most customers get instant streaming; the minority that fail get the slower, correct path.

Every node emits SSE while it runs. The customer never watches a blank screen.

---

## Guard rules

| | Catches |
|---|---|
| **G1** | Coverage language with no `check_warranty` result — forces the engine to run |
| **G1b** | A draft that upgrades the engine's negative verdict into a promise |
| **G2** | Repair steps with no retrieval behind them |
| **G3** | A SKU, price or error-code meaning that appears in no tool result *and* not in the customer's own words |
| **G4** | A safety case answered with troubleshooting steps |
| **G5** | Device-specific advice while the product is still ambiguous |
| **G6 / G6b** | Opening with a question to an upset customer; a stated deadline never acknowledged |

Guard hits are surfaced in the trace drawer rather than hidden. An agent that can show it caught itself is more convincing than one that claims it never errs.

---

## Preflight — every credential exercised, not assumed

- ✅ RKAPI `gpt-5.6-terra` — chat **and** vision, 3 keys x 8 concurrent = 24 model calls in flight. This key reaches *only* terra; there is no cheap tier.
- ✅ Pinecone `anker-support` — 3072-d cosine serverless, upsert/query/delete verified.
- ✅ Gemini `gemini-embedding-001` — 3072-d, direct Google key (RKAPI tokens are chat-only).
- ✅ Supabase — schema applied over the session pooler; the direct host is IPv6-only from here.
- ❌ Deepgram — not issued. Voice stays behind `VOICE_ENABLED=false`; nothing else depends on it.

Security note: Supabase's default privileges hand `anon` write grants on every public table. RLS blocks them, but only while RLS stays on. [`db/schema.sql`](db/schema.sql) revokes those, including from default privileges. Audited clean — no anon-writable table.

---

## Docs

- [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) — the system in full: scenarios, graph, tools, guards, warranty matrix, data model
- [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md) — wire format, shared verbatim with the frontend
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) — phases and acceptance checks
- [`docs/COST_ESTIMATE.md`](docs/COST_ESTIMATE.md) — measured unit prices and the whole-project projection
- [`db/schema.sql`](db/schema.sql) — full DDL, idempotent

---

## Notes for whoever picks this up

- **RKAPI must stream.** The same call takes 86 s non-streaming and 2.7 s streaming; non-streaming requests idle while the model reasons and trip Cloudflare's 100 s timeout. `stream=True` on every call, no exceptions.
- **RKAPI needs a real User-Agent.** Cloudflare answers a default `httpx`/`urllib` UA with `403 error code: 1010`, which looks exactly like a dead key.
- **A key is a rate lane, not a mutex.** Measured: one key pipelines 8 concurrent small calls at 2.16/s. Assuming otherwise cost a 12x latency regression that only showed up under load — see `RKAPI_PER_KEY_CONCURRENCY`.
- **`do` is a reserved word in Postgres.** `dealer_orders do` parses as the start of a DO block. It cost an hour.
- Demo commerce data is synthetic, carries `source='demo'`, and the UI labels it. No real customer data anywhere.
