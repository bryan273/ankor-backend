# Anker Hackathon · Track 4 — System Specification

> **Product name (working):** *Anker Care Agent*
> **Scope:** an after-sales AI support agent that hears the person, not just the sentence — emotion-aware, multimodal, catalog-grounded, and able to close the loop (diagnose → guide → warrant → escalate) instead of pasting a link.
> **Repos:** `anker-hackathon-backend` (this) + `anker-hackathon-frontend`.
> **Status:** built and passing. 98 unit tests, 12/12 scenario evals (S1-S4 plus edge cases), running against 860 crawled products and 463 support articles. See the README for the current numbers.
> **Companion docs:** [`API_CONTRACT.md`](API_CONTRACT.md) (wire format, shared with frontend) · [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) (phases, tasks, acceptance).

---

## 1. What the track actually asks for

The brief (赛道四 · 智能服务) names four scenarios. They are the spec — everything below exists to pass these four, plus the failure modes around them.

| # | Scenario (brief) | What the system must demonstrably do |
|---|---|---|
| S1 | Angry user photographs an error screen: *"party tomorrow, machine just died!"* | Detect anger **and** the deadline, acknowledge before troubleshooting, read the error code off the photo, then walk step-by-step with a time-boxed plan. |
| S2 | *"My S1 Pro isn't sucking anymore"* | Recognise that **S1 Pro is ambiguous** — eufy Omni S1 Pro robot vacuum vs eufy S1 Pro wearable breast pump — and disambiguate (from purchase history, or by asking with pictures) **before** answering. |
| S3 | Order number returns nothing | Recognise a **dealer/reseller** order, match against the dealer directory, decide warranty eligibility from rules, and give the one correct next step. |
| S4 | Anything vague / emotional | Judge between reassure · guide · escalate, and close the service loop with a ticket. |

Two of these are *not* LLM problems and must not be solved by the LLM alone:

- **Warranty eligibility** is a rule matrix. An LLM that "decides" warranty will eventually promise a refund the company owes nobody. → deterministic rule engine, LLM only narrates the verdict.
- **Product disambiguation** starts with a deterministic alias table (`"S1 Pro"` → 2 SKUs across 2 brands). Vector search is the fallback, not the first move.

---

## 2. Architecture at a glance

```
┌──────────────────────────── Next.js 15 (App Router, :3000) ────────────────────────────┐
│  Chat stream (left)          │  Workbench panel (right)     │  Trace drawer (judges)   │
│  · markdown + citations      │  · dynamic UI blocks         │  · stages, tools, cost   │
│  · friendly stage pills      │  · diagnostic checklist      │                          │
│  · photo drop / paste / mic  │  · product picker, warranty  │                          │
└───────────────────────────────────────┬────────────────────────────────────────────────┘
                                        │  POST /api/v1/chat  (SSE, one event vocab)
                                        │  POST /api/v1/chat/action  (block action → resume)
┌───────────────────────────────────────▼────────────────────────────────────────────────┐
│  FastAPI (:8000)  ·  ReAct agent loop  ·  SSE emitter  ·  rule engine  ·  tool registry  │
└───┬───────────────┬───────────────────┬──────────────────┬─────────────────┬───────────┘
    │               │                   │                  │                 │
┌───▼────┐   ┌──────▼──────┐   ┌────────▼────────┐  ┌──────▼──────┐  ┌───────▼────────┐
│ RKAPI  │   │  Gemini     │   │  Pinecone       │  │  Supabase   │  │  Deepgram      │
│ gpt-   │   │  embedding  │   │  anker-support  │  │  Postgres   │  │  STT/TTS       │
│ 5.6-   │   │  -001       │   │  5 namespaces   │  │  + Storage  │  │  (flagged)     │
│ terra  │   │  (3072-d)   │   │                 │  │             │  │                │
│ text + │   └─────────────┘   └─────────────────┘  └─────────────┘  └────────────────┘
│ vision │
└────────┘
```

### 2.1 Stack decisions and why

| Layer | Choice | Reason |
|---|---|---|
| Backend | **Python 3.11 + FastAPI** | async SSE, Pydantic v2 schemas double as the UI-block contract |
| Agent | **Explicit async ReAct loop** (`app/agent/graph.py`) | *Changed during the build; LangGraph was the plan.* The deciding factor was streaming: every node emits SSE while it runs — stage pills, friendly narration, per-tool call and result events — and driving that through a graph framework's callback layer costs more control than its scheduling is worth for a linear pipeline with one loop. The one thing LangGraph would have given us free is interrupt/resume, so that is built explicitly: `disambiguate` writes a checkpoint to Postgres and `/chat/action` resumes the same turn. Surviving a page reload was the requirement, and an in-memory graph would not have. |
| LLM | **`gpt-5.6-terra` via RKAPI** (`https://cdn.rkapi.com/v1`, OpenAI-compat) | verified working; reasoning + vision in one model, cheap on credits |
| Vision | same model, `image_url` data-URI parts | verified working — no second provider needed |
| Embeddings | **`gemini-embedding-001`**, 3072-d, direct Google AI Studio key | RKAPI tokens are chat-only (verified 403). Fallback: OpenAI `text-embedding-3-large` (3072-d, same index shape — swap without re-indexing dimension) |
| Vectors | **Pinecone serverless**, index `anker-support`, cosine, 3072-d | provided key; namespaces isolate corpora |
| Relational | **Supabase Postgres** | orders, tickets, sessions, checkpoints, product mirror; Storage for user photos |
| Frontend | **Next.js 15 + TS + Tailwind + shadcn/ui** | generative-UI block registry maps cleanly to React components |
| Voice | **Deepgram** STT/TTS behind `VOICE_ENABLED` | key pending; Web Speech API stub so the demo never blocks on it |
| Observability | structured JSON logs + `/metrics` + a per-turn trace row | the trace drawer is a judging asset, not just debug |

### 2.2 Non-goals

No deployment (local only), no real payment/RMA integration, no real customer PII, no fine-tuning. Order/dealer/ticket data is **seeded demo data**, clearly labelled as such in the UI footer.

---

## 3. The agent

### 3.1 Graph

```
                       ┌──────────────┐
  user turn ─────────▶ │   ingest     │  attachments → VLM facts, normalise
                       └──────┬───────┘
                              ▼
                       ┌──────────────┐
                       │   rewrite    │  history-aware query rewrite ("it", "that one")
                       └──────┬───────┘
                              ▼
                       ┌──────────────┐
                       │   perceive   │  ONE call → emotion + urgency + intent + entities
                       └──────┬───────┘
                              ▼
                       ┌──────────────┐   ambiguous?  ┌─────────────────────┐
                       │ disambiguate │──────────────▶│ interrupt:          │
                       └──────┬───────┘   yes         │ ui_block product_   │
                              │ no                    │ picker → wait       │
                              ▼                       └─────────────────────┘
                    ┌───────────────────┐
             ┌─────▶│  plan  (ReAct)    │  think → pick tool → observe → reflect
             │      └─────────┬─────────┘  max 6 iterations, budget-capped
             │                ▼
             │      ┌───────────────────┐
             └──────│   act  (tools)    │  11 tools, §3.4
       loop         └─────────┬─────────┘
                              ▼
                       ┌──────────────┐
                       │    guard     │  rule constraints, §3.5 — can force a re-plan
                       └──────┬───────┘
                              ▼
                       ┌──────────────┐
                       │   compose    │  empathy layer + answer + blocks + suggestions
                       └──────┬───────┘
                              ▼
                       ┌──────────────┐
                       │   persist    │  messages, blocks, tool traces, ticket state
                       └──────────────┘
```

Every node emits SSE as it runs. The user never watches a blank screen: `perceive` emits `status`, `plan` emits friendly `thinking_delta`, each tool emits `tool_call`/`tool_result`, `compose` streams `content_delta`.

### 3.2 `perceive` — one call, four outputs

A single structured-output call returns:

```jsonc
{
  "emotion": "angry",              // calm | confused | frustrated | angry | anxious | happy
  "intensity": 0.8,                // 0..1
  "urgency": {                     // extracted, not guessed
    "has_deadline": true,
    "deadline_hint": "tomorrow evening — party",
    "level": "high"
  },
  "intent": "troubleshoot",        // §3.3
  "entities": {
    "product_mentions": ["S1 Pro"],
    "error_codes": ["E-05"],
    "order_refs": [],
    "purchase_channel_hint": null
  },
  "language": "en",                // reply language follows the user
  "needs_image": false
}
```

Why one call: three separate classifier calls triple latency for no accuracy gain, and the fields are mutually informative (a deadline changes what counts as "angry").

**Emotion → policy table** (deterministic, applied in `compose`):

| Emotion | Opening | Step granularity | Escalate threshold | Extra |
|---|---|---|---|---|
| calm | none, answer directly | normal | 3 failed steps | — |
| confused | one orienting line | fine, one action per step | 3 | offer picture-per-step |
| frustrated | acknowledge + "let's fix this" | fine | 2 | offer human handoff up front |
| angry | acknowledge **first**, no jargon, no upsell | fine, ≤3 steps then check in | **1** | never ask for info already given |
| anxious + deadline | acknowledge deadline, give a time-boxed plan | fastest-path first | 2 | offer fallback ("if this fails by 6pm, here's plan B") |

Rule: when `emotion ∈ {angry, frustrated}` the composer is forbidden from opening with a question. It acknowledges, then acts.

### 3.3 Intents

`troubleshoot` · `product_question` · `order_status` · `warranty_claim` · `return_refund` · `buy_advice` · `how_to` · `complaint` · `chitchat` · `escalate_request` · `unclear`

`unclear` does **not** mean "ask a generic clarifying question". It means: run one cheap retrieval, and ask a question that offers concrete options (a `product_picker` or `quick_replies` block), never an open "could you clarify?".

### 3.4 Tools

| Tool | Signature | Notes |
|---|---|---|
| `search_products` | `(query, brand?, category?, k=8)` | alias table first, then vector over `products` namespace |
| `get_product` | `(sku)` | specs, price, media, manual links from Supabase |
| `search_kb` | `(query, product_id?, doc_type?)` | RAG over `kb` namespace; returns chunks **with citations** |
| `get_troubleshooting_flow` | `(symptom, product_id)` | structured step list → renders as a `diagnostic_steps` block |
| `lookup_order` | `(order_no? , email?, phone?)` | Supabase; returns `not_found` distinctly from `error` |
| `lookup_dealer_order` | `(order_no, dealer_hint?)` | dealer directory match by prefix/format/name fuzzy |
| `check_warranty` | `(sku, purchase_date, channel, damage_class)` | **rule engine, no LLM** → §3.5 |
| `search_tickets` | `(query, k=5)` | historical resolved tickets, `tickets` namespace |
| `analyze_image` | `(attachment_id, question)` | second-pass VLM on an already-uploaded photo |
| `web_search` | `(query)` | Tavily, **domain-allowlisted** to anker.com/eufy.com/soundcore.com/support.anker.com |
| `create_ticket` / `escalate` | `(summary, priority, reason, transcript_ref)` | closes the loop; returns a ticket id the UI renders |

Tool results are typed Pydantic models. The ReAct loop sees a compact JSON view; the composer sees the same object and may lift fields into UI blocks.

### 3.5 Guardrails (the `guard` node)

Hard constraints, checked programmatically after the ReAct loop and **before** composing:

1. **No warranty verdict without `check_warranty`.** If the draft mentions coverage/refund/replacement and no `check_warranty` result is in state → force re-plan.
2. **No unsourced technical claim.** Any troubleshooting step must trace to a `search_kb` / `get_troubleshooting_flow` result, or be marked as a general suggestion.
3. **No invented SKU, price, spec, or error-code meaning.** Values must appear in a tool result; the composer receives a whitelist.
4. **Safety escalation.** Swelling battery, burning smell, smoke, water ingress on a mains device, injury → skip troubleshooting entirely, emit safety instructions + `escalate(priority=urgent)`.
5. **Product must be resolved** before any product-specific instruction is emitted.
6. **Emotion policy honoured** (no question-opener when angry; deadline acknowledged when present).

Each violation is logged with the rule id — the trace drawer shows guard hits, which is a strong demo of "orchestration + rule constraints" (the phrase the brief uses).

### 3.6 Warranty rule matrix

Inputs: `channel` × `purchase_date` × `product warranty term` × `damage_class` (from VLM: `defect` | `physical_damage` | `wear` | `unknown`) × `proof_present`.

| Channel | Order found | Proof | Within term | Damage class | Verdict |
|---|---|---|---|---|---|
| official store | yes | implicit | yes | defect | `covered` |
| official store | yes | implicit | yes | physical_damage | `not_covered_policy` (offer paid repair) |
| official store | yes | implicit | no | any | `expired` (offer paid repair / trade-in) |
| marketplace (Amazon etc.) | no | uploaded | yes | defect | `covered_pending_verification` |
| **dealer** | not in system | none | unknown | any | `needs_proof` → ask for invoice + dealer name → `lookup_dealer_order` |
| dealer | matched in directory | invoice | yes | defect | `covered_via_dealer` (+ route to that dealer's service path) |
| unknown | no | none | unknown | any | `escalate_human` |

The engine returns `{verdict, reason_code, required_evidence[], next_action}`. The LLM only phrases it. This is scenario **S3** solved deterministically.

### 3.7 Disambiguation (scenario S2)

```
"S1 Pro isn't sucking anymore"
        │
        ├─ alias table lookup "s1 pro" → [eufy Omni S1 Pro (robovac), eufy S1 Pro (breast pump)]
        │
        ├─ discriminators, in order:
        │    1. purchase history for this customer  → if exactly one owned → resolve silently, state the assumption
        │    2. symptom vocabulary ("suction/nggak nyedot" fits both; "mopping" → vacuum; "milk/let-down" → pump)
        │    3. attached photo → VLM identifies form factor
        │    4. still ≥2 → emit `product_picker` block with photo + one-line description, and PAUSE
        │
        └─ resume on user action → continue from `plan` with resolved sku
```

Silent resolution always says so in one clause ("Going by your order from March, this is the Omni S1 Pro robot vacuum — tell me if I've got the wrong one"), so a wrong guess is cheap to correct.

### 3.8 Proactivity

Every composed answer carries three things beyond the answer itself:

- `next_steps` — what the agent will do or the user should do next, concrete.
- `suggestions` — 2–3 follow-up questions the user probably has but hasn't asked (rendered as chips).
- optional **unprompted value** — a related known issue, a firmware update, a maintenance reminder for that SKU. Capped at one per turn so it never becomes noise.

---

## 4. Data

### 4.1 Sources (verified crawlable)

`robots.txt` checked live — product and collection paths are not disallowed on either domain, and both expose product sitemaps:

| Domain | Sitemap | Content |
|---|---|---|
| `anker.com` | `/server-sitemap-index-products.xml`, `/server-sitemap-index-collections.xml` | chargers, power banks, cables |
| `eufy.com` | `/server-sitemap-index-products.xml`, `/server-sitemap-index-blog.xml`, `/sitemap_agentic_discovery.xml` | robot vacuums, security, **eufy Baby (breast pumps)** — both halves of the S1 Pro ambiguity |
| `soundcore.com` | products sitemap | audio |
| `ankersolix.com`, `nebula` , `ankerwork.com` | products sitemap | power stations, projectors, webcams |
| `support.anker.com` | crawl from product pages | FAQ, troubleshooting articles, **manual PDFs** |
| `community.anker.com` | targeted queries only | real user-phrased symptoms — gold for retrieval recall |

Crawl etiquette: 1–2 req/s per host, `User-Agent` identifying the hackathon project, on-disk cache so re-runs cost nothing, respect `Disallow`, no login-gated pages.

### 4.2 Pipeline

```
sitemap → url filter → fetch (cached) → extract → normalise → media → chunk → embed → upsert
                                          │
                                          ├─ JSON-LD `Product` schema first (name, sku, price, image, brand)
                                          └─ DOM fallback (spec tables, bullets)
```

- **Images**: hero + gallery downloaded, captioned by the VLM into an attribute string (`"white cylindrical wearable breast pump, single cup, magnetic charging case"`). Caption is what gets embedded — text-space multimodal. Cheap, works with any text embedder, and the image URL rides along in metadata so a retrieved chunk can render a picture.
- **Manuals**: PDF → text (PyMuPDF) → section-aware chunks; error-code tables extracted into a dedicated `error_codes` table (deterministic lookup beats RAG for `E-05`).
- **Chunking**: ~800 tokens, 120 overlap, never across a heading boundary; each chunk keeps `{brand, sku, doc_type, url, section, page}`.
- **Embedding**: `gemini-embedding-001`, `task_type=RETRIEVAL_DOCUMENT` for chunks / `RETRIEVAL_QUERY` for queries, 3072-d. Content-hash per chunk so a re-crawl only pays for what changed.

### 4.3 Pinecone namespaces (index `anker-support`, cosine, 3072-d)

| Namespace | Unit | Metadata |
|---|---|---|
| `products` | one product = title + short desc + key specs + image caption | brand, sku, category, price, url, image_url |
| `kb` | manual/FAQ/troubleshooting chunk | sku[], doc_type, url, section, page |
| `tickets` | historical resolved ticket (problem + resolution) | sku, symptom, resolution_type |
| `dealers` | dealer profile + regions + order-number formats | region, dealer_id |
| `community` | community Q&A pair | sku, votes |

### 4.4 Supabase schema (core tables)

```
products(id, brand, sku, name, slug, category, price, currency, url, hero_image, status, raw jsonb)
product_aliases(id, alias, product_id, confidence)        -- "S1 Pro" → 2 rows. Drives §3.7
product_specs(product_id, key, value, unit)
product_media(id, product_id, url, kind, caption, vlm_tags jsonb)
product_docs(id, product_id, kind, url, local_path, parsed_text)
error_codes(id, product_id, code, meaning, severity, fix_steps jsonb)
kb_articles(id, source_url, title, doc_type, product_ids uuid[], body)
kb_chunks(id, article_id, ord, text, text_hash, pinecone_id, meta jsonb)
troubleshooting_flows(id, product_id, symptom, steps jsonb)

customers(id, email, phone, name, locale)
orders(id, order_no, customer_id, channel, purchase_date, status, total)
order_items(id, order_id, product_id, qty, serial)
dealers(id, name, region, order_no_pattern, contact, service_path)
dealer_orders(id, dealer_id, order_no, product_id, purchase_date, customer_ref)
warranty_policies(id, category, months, covers jsonb, excludes jsonb)

sessions(id, customer_id, locale, created_at, meta jsonb)
messages(id, session_id, role, text, emotion, intent, created_at)
message_blocks(id, message_id, block_id, type, payload jsonb, actions jsonb, state)
attachments(id, session_id, storage_path, mime, vlm_facts jsonb)
tool_traces(id, message_id, tool, args jsonb, result jsonb, ms, ok)
guard_hits(id, message_id, rule_id, detail)
tickets(id, session_id, customer_id, product_id, priority, status, summary, verdict)
ticket_events(id, ticket_id, kind, payload jsonb, created_at)
sessions.meta->'checkpoint'                                -- paused-turn state (jsonb)
eval_cases(id, name, scenario, input jsonb, expect jsonb)
eval_runs(id, case_id, passed, score jsonb, transcript jsonb, created_at)
```

Demo data (orders, dealers, tickets, customers) is generated by `scripts/seed_demo.py` — 12 dealers, ~120 orders, ~200 historical tickets, deliberately including the S3 case (an order number that exists **only** in `dealer_orders`).

RLS: the backend uses the service role; the anon key is read-only over `products` for public browsing.

---

## 5. Retrieval

Two-stage, cheap first:

1. **Deterministic layer** — alias table, `error_codes` exact match, order-number format match. If this resolves the question, no vector call happens at all.
2. **Vector layer** — namespace routed by intent; for troubleshooting the query is HyDE-expanded ("write the manual paragraph that would answer this") because user symptom language ("nggak nyedot", "won't suck") is far from manual language ("reduced suction performance"). Top-30 → LLM listwise rerank → top-6 into context.
3. **Filters** — always constrain by `sku` once the product is resolved. A robot-vacuum answer leaking into a breast-pump thread is the single worst failure this system can make.

Every KB-derived sentence carries `[n]` citations resolved to `{title, url, section}`; the UI renders them as hoverable chips.

---

## 6. Streaming contract

One SSE vocabulary, emitted by FastAPI, parsed by one `useEventStream` hook in Next.js. Full payload schemas in [`API_CONTRACT.md`](API_CONTRACT.md).

`status` · `stage_start` · `stage_complete` · `thinking_delta` · `tool_call` · `tool_result` · `emotion` · `content_delta` · `content_reset` · `ui_block` · `citation` · `suggestions` · `ticket_update` · `usage` · `complete` · `error`

Rules learned the hard way on a prior project and adopted here:

- **A rename here touches both repos.** The vocabulary is versioned (`X-Event-Vocab: 1`) and changes go through `API_CONTRACT.md` first.
- `thinking_delta` is **user-friendly narration**, never raw chain-of-thought ("Checking your order history…", not "The user's utterance implies…"). Raw reasoning is logged server-side only.
- Every `stage_start` must get a matching `stage_complete` **or** `stage_error` on every code path, including early returns. A stage that never closes is a spinner that never stops.
- `error` carries a machine `code` (`RATE_LIMITED`, `UPSTREAM_5XX`, `GUARD_BLOCKED`) so the UI can react rather than print a stack.

---

## 7. Dynamic / generative UI

The server never sends HTML. It sends **typed blocks**; the frontend owns a registry `type → React component`. Adding a UI affordance = adding one Pydantic model + one component.

| Block `type` | When | Actions it can send back |
|---|---|---|
| `product_picker` | ≥2 candidate SKUs | `select_product{sku}` |
| `diagnostic_steps` | any guided fix | `step_result{step_id, outcome: worked\|failed\|stuck}` |
| `order_card` | order found | `use_order{order_no}` |
| `warranty_result` | after `check_warranty` | `start_claim`, `upload_proof`, `talk_to_human` |
| `upload_request` | photo or receipt needed | `attachment{id}` |
| `product_grid` / `product_card` | recommendation, accessory, replacement part | `open_product{sku}`, `add_to_cart` (demo) |
| `comparison_table` | "which one should I get" | `select_product{sku}` |
| `ticket_status` | after escalation | `add_note`, `cancel_ticket` |
| `human_handoff` | escalation | `confirm_handoff`, `stay_with_ai` |
| `form` | RMA / address / serial | `submit{fields}` |
| `quick_replies` | every turn | `reply{text}` |
| `link_list` / `video_guide` | manuals, how-to | `open{url}` |

Envelope:

```jsonc
{
  "block_id": "blk_7f3a",
  "type": "diagnostic_steps",
  "payload": { "...": "typed per block" },
  "actions": [ { "id": "step_result", "label": "It worked", "value": {"outcome": "worked"} } ],
  "state": "active"          // active | answered | expired
}
```

A user action `POST /api/v1/chat/action` reloads the checkpoint from `sessions.meta`, marks the block answered, injects the action as an observation, and continues from the ReAct node. It does not re-run the turn, so the customer never repeats themselves.

**Layout**: chat on the left, workbench on the right. Short answers (a fact, a yes/no, a quick fix) stay inline in chat. Long-lived artefacts (a 7-step diagnostic, a warranty verdict, a ticket timeline) render in the workbench with a one-line summary bubble in chat, so the conversation stays readable while the work stays visible.

---

## 8. Multimodal handling

1. Upload (`POST /api/v1/attachments`) → Supabase Storage → immediate VLM pass →

```jsonc
{
  "caption": "eufy robot vacuum docking station, display shows E-05",
  "detected": { "brand": "eufy", "form_factor": "robot_vacuum", "error_code": "E-05",
                "damage_class": "defect", "confidence": 0.82 },
  "ocr_text": "Error E-05  Please check the brush",
  "safety_flags": []
}
```

2. Those facts are **merged with the text turn before routing** — the router sees the image, not just the sentence. (Skipping this is a known failure: a pasted photo with "what is this?" falls through to a canned clarification.)
3. Facts feed the alias table (form factor discriminates S1 Pro), the error-code table (exact lookup), the warranty engine (`damage_class`), and the guard (safety flags).
4. The UI echoes the caption back — "I can see the E-05 on the dock" — so the user knows the photo landed.

---

## 9. Voice (flagged)

`VOICE_ENABLED=false` until the Deepgram key arrives. Design: mic → Deepgram streaming STT over WebSocket → interim transcript rendered live → final transcript submitted as a normal turn; answers optionally read back with Deepgram TTS (`aura-2`). Web Speech API is the fallback so the demo path never depends on a missing key.

---

## 10. Performance and 50-user load

| Target | Value |
|---|---|
| First SSE event | < 400 ms |
| First `content_delta` | p95 < 4 s |
| Full answer (no tools) | p95 < 8 s |
| Full answer (2–3 tools) | p95 < 25 s |
| Concurrent users | 50 sustained |

Mechanics:

- **Key lanes.** Two requests on one RKAPI key serialise (~1.9× latency); separate keys run parallel (~1.1×). Six openai-group keys → a 6-lane `asyncio.Semaphore` pool with round-robin assignment. Pool size *is* the throughput ceiling.
- Retrieval, VLM captioning, and history load run concurrently in `ingest`.
- Cache: embeddings by content hash, KB retrieval by (query-hash, sku) for 5 min, product lookups in-process LRU.
- Backpressure: queue depth > lanes × 3 → return `error{code:"BUSY"}` with a polite retry block rather than timing out silently.
- Load test: k6 (or Locust) with 50 VUs replaying the golden scenarios, asserting p95 and zero unclosed stages.

---

## 11. Evaluation

**Golden set**: 50–60 cases in `eval_cases`, covering S1–S4 plus edge cases (empty order, wrong product photo, safety hazard, non-English input, abusive user, question outside catalog).

Each case asserts both:

- **Deterministic** — which tools were called, whether the product resolved to the right SKU, warranty verdict equals the expected enum, no guard violation, citation count > 0 when KB was used.
- **Judged** — an LLM judge scores empathy, clarity, proactivity, and hallucination on a 1–5 rubric against the transcript.

A run writes to `eval_runs`; `/admin/eval` renders pass rate per scenario with drill-down into transcripts. This page is also the judge-facing proof that the thing works.

---

## 12. Credentials — verified status

Probed live 2026-09-09 / 2026-09-10. Everything marked ✅ was exercised end to end, not read off a docs page.

| Credential | Result | State |
|---|---|---|
| RKAPI openai-group keys ×3 | ✅ `gpt-5.6-terra` chat, usage reported on all three | ready; wire the 6-key lane pool |
| RKAPI vision | ✅ `image_url` data-URI part accepted and answered correctly | this is the VLM |
| RKAPI model access | ⚠️ **`gpt-5.6-terra` only.** `gpt-5.4-mini` → *"not supported when using Codex with a ChatGPT account"*; `gpt-5.4-nano`, `gpt-4.1-nano`, `gpt-4o-mini`, `gemini-3-flash-preview`, `claude-haiku` → `403 no access` | no cheap classifier tier — see [`COST_ESTIMATE.md`](COST_ESTIMATE.md) §5 |
| RKAPI embeddings | ❌ `403 no access` for every embedding model | embeddings do not go through RKAPI |
| **Pinecone** | ✅ **index `anker-support` created** — 3072-d, cosine, serverless aws/us-east-1, `ready:true`, host `anker-support-sle6cac.svc.aped-4627-b74a.pinecone.io`. Upsert → query → delete roundtrip passed | **done** |
| **Gemini embeddings** | ✅ `gemini-embedding-001` → 3072-d and `gemini-embedding-2-preview` → 3072-d, both with `taskType=RETRIEVAL_DOCUMENT` | ready; `-001` is the default (it batches; `-2-preview` is single-input) |
| Supabase `sb_secret_…` | ✅ accepted by PostgREST — insert, nested-join select and delete all verified against real tables | ready for all runtime data access |
| **Supabase DDL** | ✅ **schema applied.** Direct `db.<ref>.supabase.co` is IPv6-only and unreachable from here; the **session-mode pooler** `aws-0-ap-northeast-1.pooler.supabase.com:5432` as `postgres.<ref>` works. PostgreSQL 17.6. `db/schema.sql` ran clean and is idempotent | **done** — 25 tables live, `warranty_policies` seeded |
| OpenAI direct key | available | fallback embedder (`text-embedding-3-large`, same 3072-d) |
| Tavily | available | optional allowlisted web search |
| Deepgram | ❌ not issued | voice stays behind `VOICE_ENABLED=false` |

**Gotcha, cost an hour:** RKAPI sits behind Cloudflare and answers a default `urllib`/`curl` User-Agent with `HTTP 403  error code: 1010`. The openai SDK works because it sends its own UA. Any hand-rolled HTTP call must set a `User-Agent`. This looks exactly like a dead key and is not one.

### Nothing blocks the build

Every dependency the pilot needs is live and exercised. `scripts/db_bootstrap.py` probes all three Postgres routes, applies `db/schema.sql`, and verifies the result; re-running it is safe.

**Security note from the first apply.** Supabase enables RLS on new public tables automatically, *and* its default privileges hand `anon` table-level `SELECT` **and** `INSERT/UPDATE/DELETE` on every one of them. RLS blocks the writes today, so nothing leaks — but that safety is one `disable row level security` away from a public write endpoint on `orders`. The schema now revokes those write grants from `anon` and `authenticated`, and revokes them from default privileges so future tables inherit the tighter setting. Re-audited after applying: no RLS-off table is anon-readable, and no table is anon-writable.

Still open, neither blocking:

- **Deepgram key** — voice only; everything else ships without it.
- Whether the RKAPI **claude-group** key is available to this project. It would put `perceive` and rerank on `claude-haiku-4-5` at roughly ⅓ the cost. One probe settles it.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| Crawl yields thin product text | JSON-LD first, DOM fallback, and manuals carry the technical weight regardless |
| RKAPI rate limits under 50 VUs | 6-key lane pool + queue + `BUSY` backpressure; cache aggressively |
| Reasoning model latency | stream stages early; run retrieval before/parallel to the ReAct loop; cap iterations at 6 |
| Vector answers leak across products | hard `sku` filter post-resolution + guard rule 5 |
| Ambiguity resolved wrongly and silently | always state the assumption in one clause + one-tap correction |
| Demo data mistaken for real | footer label, `channel="demo"` on every seeded order |
| Event-vocab drift between repos | contract doc is the source of truth; a typed TS mirror is generated from the Pydantic models |
