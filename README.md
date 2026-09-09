# Anker Hackathon — Track 4: Intelligent Services · Backend

> Anker 首届黑客松挑战赛 · 赛道 04 智能服务（真正听懂，真正解决）
> **Track 4 — Intelligent Services:** an AI-powered after-sales customer-service agent + a highly interactive front-end page for real after-sales scenarios.

## Mission (from the track brief)

Integrate user text, fault images, a technical knowledge base, and historical work-order information to perform **emotion recognition → product disambiguation → fault location → troubleshooting guidance → escalation**, orchestrated as a complete business flow with an LLM agent + rule constraints.

Key scenarios to prove:
- Angry user photos an error message before a party → address the emotion first, then guide step-by-step.
- "My S1 Pro isn't pumping anymore" → disambiguate **breast pump vs robot vacuum** before answering.
- Order not found in system → recognize dealer order, follow dealer list, decide warranty coverage and next action.
- Judge when to offer reassurance, guidance, or escalation — close the loop, don't just respond.

## Scope of this repo

Agent backend / orchestration:

- LLM agent with process orchestration + rule constraints (guardrails)
- Emotion recognition (text + image context)
- Product disambiguation against the product catalog
- Multimodal fault-image understanding
- Knowledge-base retrieval (technical KB)
- Work-order / order lookup (incl. dealer-order path)
- Escalation decisioning and service-loop state

Frontend lives in the sibling repo **`anker-hackathon-frontend`**.

## Specs

- [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) — system design: agent graph, guardrails, warranty rule matrix, data pipeline, retrieval, credential status
- [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md) — endpoints, SSE vocabulary, UI-block schemas. **Shared with the frontend repo; a change lands in both.**
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) — phases, task board, per-scenario definition of done
- [`docs/COST_ESTIMATE.md`](docs/COST_ESTIMATE.md) — measured unit prices, per-turn token budget, whole-project projection, cost levers
- [`db/schema.sql`](db/schema.sql) — full DDL, paste-once into the Supabase SQL editor

## Stack

Python 3.11 · FastAPI + `sse-starlette` · **LangGraph** (ReAct + supervisor + `interrupt`/resume on a
Postgres checkpointer — the brief needs the agent to pause and ask mid-reasoning) · Pydantic v2 schemas
that double as the UI-block contract.

| Concern | Choice | Note |
|---|---|---|
| LLM + vision | `gpt-5.6-terra` via RKAPI (`https://cdn.rkapi.com/v1`, OpenAI-compat) | verified: chat **and** `image_url` parts |
| Embeddings | `gemini-embedding-001`, 3072-d, direct Google AI Studio key | RKAPI tokens are chat-only (verified 403) |
| Vectors | Pinecone serverless, index `anker-support`, cosine 3072-d | namespaces: products · kb · tickets · dealers · community |
| Relational | Supabase Postgres + Storage | orders, tickets, sessions, checkpoints, product mirror |
| Voice | Deepgram STT/TTS behind `VOICE_ENABLED` | key pending |

> **Gotcha:** RKAPI sits behind Cloudflare and answers a default `curl`/`urllib` User-Agent with
> `HTTP 403  error code: 1010`. The openai SDK works because it sends its own UA. Any hand-rolled HTTP
> call must set a `User-Agent` — this looks exactly like a dead key and is not one.

## Dev setup

```bash
python -m venv .venv && source .venv/Scripts/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                      # see API_CONTRACT.md §7
alembic upgrade head                                      # needs SUPABASE_SERVICE_ROLE_KEY
python scripts/seed_demo.py
uvicorn app.main:app --reload --port 8000
```

## Preflight status

Probed live — see [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) §12 for the full table.

- ✅ RKAPI `gpt-5.6-terra` — chat **and** vision, on all three keys. Note: this key reaches *only* terra, no cheaper tier.
- ✅ Pinecone index **`anker-support` created** — 3072-d cosine serverless, upsert/query/delete roundtrip passes.
- ✅ Gemini embeddings — `gemini-embedding-001` and `gemini-embedding-2-preview`, both 3072-d.
- ✅ Supabase `sb_secret_` key — insert, nested-join select and delete verified against real tables.
- ✅ **Schema applied** — 25 tables live on PostgreSQL 17.6, `warranty_policies` seeded. Direct `db.<ref>` is IPv6-only from here; the session-mode pooler is the working route.

**Nothing is blocked.** Re-run the bootstrap any time — it is idempotent:

```bash
export SUPABASE_REF=... SUPABASE_DB_PASSWORD=... SUPABASE_REGION=ap-northeast-1
python scripts/db_bootstrap.py --apply     # probe routes, apply db/schema.sql, verify
```

Security note: Supabase's default privileges give `anon` write grants on every public table (RLS blocks them, but only while RLS stays on). `db/schema.sql` revokes those, including from default privileges. Audited clean — no anon-writable table, no RLS-off table readable by anon.

Costs are modelled in [`docs/COST_ESTIMATE.md`](docs/COST_ESTIMATE.md): ≈ $0.12 credit per troubleshooting turn, ≈ **$493 credit / ~$15 real money** for the entire pilot including dev, eval, a 50-user load test and demo day. Infra runs on free tiers.
