# API Contract — Anker Care Agent

> **This file is the single source of truth shared by `anker-hackathon-backend` and `anker-hackathon-frontend`.**
> A change here lands in both repos in the same PR. Event names and block types are versioned: responses carry `X-Event-Vocab: 1`.
> Backend defines these as Pydantic v2 models in `app/schemas/`; `scripts/gen_ts_types.py` emits `frontend/types/contract.ts` so the TypeScript side cannot drift.

---

## 1. Conventions

- Base URL: `http://localhost:8000`, prefix `/api/v1`.
- Auth (demo): header `X-API-Key: <BACKEND_API_KEY>`. Identity comes from `session_id`; anonymous sessions are allowed.
- All JSON is `snake_case`.
- Timestamps are ISO-8601 UTC.
- Errors are `{"error": {"code": "...", "message": "...", "detail": {...}}}` with the HTTP status matching the class of failure.

| `code` | HTTP | Meaning |
|---|---|---|
| `BAD_REQUEST` | 400 | schema/validation |
| `UNAUTHORIZED` | 401 | missing/incorrect API key |
| `NOT_FOUND` | 404 | unknown session/order/product |
| `BUSY` | 429 | all model lanes saturated — retry with backoff |
| `RATE_LIMITED` | 429 | upstream provider limit |
| `UPSTREAM_5XX` | 502 | model/vector/db upstream failed |
| `GUARD_BLOCKED` | 422 | the answer violated a hard rule and could not be repaired |

---

## 2. Endpoints

### 2.1 `POST /api/v1/chat` — streaming turn (SSE)

Request:

```jsonc
{
  "session_id": "sess_01J...",          // omit to create a new session; the id comes back in `status`
  "message": "my S1 Pro isn't sucking anymore, party tomorrow!!",
  "attachment_ids": ["att_9f2"],        // uploaded beforehand via /attachments
  "locale": "en",                        // hint only; the agent replies in the user's detected language
  "client_context": {                    // optional, all fields optional
    "customer_email": "demo@anker.test",
    "timezone": "Asia/Jakarta",
    "entry_point": "web"
  }
}
```

Response: `text/event-stream`, events per §3. The stream always terminates with exactly one `complete` **or** one `error`.

### 2.2 `POST /api/v1/chat/action` — resume from a UI block (SSE)

Sent when the user interacts with a block. Resumes the paused graph from its checkpoint; it does **not** replay the turn.

```jsonc
{
  "session_id": "sess_01J...",
  "block_id": "blk_7f3a",
  "action_id": "select_product",
  "value": { "sku": "T8D04121" }
}
```

Response: same SSE stream.

### 2.3 `POST /api/v1/attachments` — upload (multipart)

`file` (image/jpeg|png|webp|heic, ≤ 10 MB) + `session_id`.

```jsonc
{
  "attachment_id": "att_9f2",
  "url": "https://<supabase>/storage/v1/object/sign/...",
  "vlm_facts": {
    "caption": "eufy robot vacuum dock, display shows E-05",
    "ocr_text": "Error E-05  Please check the brush",
    "detected": { "brand": "eufy", "form_factor": "robot_vacuum",
                  "error_code": "E-05", "damage_class": "defect", "confidence": 0.82 },
    "safety_flags": []
  }
}
```

The VLM pass runs synchronously so the next `/chat` call already has the facts. Typical 1.5–3 s; the UI shows the thumbnail immediately and the caption when it lands.

### 2.4 Read endpoints

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/v1/sessions/{id}` | session meta + resolved product + open ticket |
| `GET` | `/api/v1/sessions/{id}/messages?limit=50` | messages with their blocks, for rehydrating a reload |
| `GET` | `/api/v1/products?q=&brand=&category=&limit=` | catalog search (also powers `/products`) |
| `GET` | `/api/v1/products/{sku}` | full product: specs, media, manuals, known error codes |
| `GET` | `/api/v1/orders/{order_no}?email=` | order or `404 NOT_FOUND` (dealer path is agent-only) |
| `GET` | `/api/v1/tickets/{id}` | ticket + event timeline |
| `POST` | `/api/v1/tickets` | manual ticket creation (used by `human_handoff`) |
| `GET` | `/api/v1/eval/runs?scenario=` | eval results for `/admin/eval` |
| `GET` | `/healthz` | `{status, version, deps:{rkapi, pinecone, supabase, embed}}` |
| `GET` | `/metrics` | Prometheus text |

### 2.5 `POST /api/v1/voice/transcribe` *(flagged)*

Multipart audio → `{text, confidence, language}`. Returns `503 {"code":"VOICE_DISABLED"}` while `VOICE_ENABLED=false`.

---

## 3. SSE event vocabulary (v1)

Every event: `event: <name>` + `data: <json>`. Unknown events must be ignored by the client, never fatal.

| Event | Payload | Notes |
|---|---|---|
| `status` | `{session_id, message_id, state:"started"}` | always first; carries the id for a new session |
| `stage_start` | `{stage_id, label}` | `label` is user-facing ("Reading your photo") |
| `stage_complete` | `{stage_id, ms}` | **must** fire for every `stage_start` on every path |
| `stage_error` | `{stage_id, code, message}` | closes a stage that failed; the turn may still continue |
| `emotion` | `{emotion, intensity, urgency}` | lets the UI soften its tone/colour |
| `thinking_delta` | `{stage_id, delta}` | friendly narration only — never raw chain-of-thought |
| `tool_call` | `{call_id, tool, label, args_preview}` | `label` = "Checking your order history" |
| `tool_result` | `{call_id, ok, ms, summary}` | `summary` is short and human; full result stays server-side |
| `content_delta` | `{delta}` | the answer, token by token |
| `content_reset` | `{}` | discard what was streamed and restart (retry after a guard repair) |
| `ui_block` | block envelope, §4 | may arrive mid-answer; render in the workbench |
| `citation` | `{n, title, url, section, sku?}` | one event per citation, `[n]` matches the text |
| `suggestions` | `{items:[{text}]}` | follow-up chips |
| `ticket_update` | `{ticket_id, status, priority, summary}` | after `create_ticket`/`escalate` |
| `usage` | `{input, output, total, cost_credits, model}` | shown in the trace drawer |
| `complete` | `{message_id, blocks:[block_id], resolved_sku?, ticket_id?, awaiting_action?}` | terminal; `awaiting_action:true` means the graph paused on a block |
| `error` | `{code, message, retryable}` | terminal |

Ordering guarantees: `status` first; `emotion` before the first `content_delta`; `citation` events before `complete`; exactly one terminal event.

---

## 4. UI block schemas

Envelope:

```jsonc
{
  "block_id": "blk_7f3a",
  "type": "diagnostic_steps",
  "payload": {},
  "actions": [{ "id": "step_result", "label": "It worked", "style": "primary", "value": {"outcome":"worked"} }],
  "state": "active"                      // active | answered | expired
}
```

### `product_picker`
```jsonc
{"question": "Which S1 Pro do you have?",
 "options": [
   {"sku":"T2080","name":"eufy Robot Vacuum Omni S1 Pro","brand":"eufy","image_url":"...",
    "hint":"Robot vacuum with mop, docks in a station","price":1499.99},
   {"sku":"T8D04121","name":"eufy Wearable Breast Pump S1 Pro","brand":"eufy Baby","image_url":"...",
    "hint":"Wearable pump with a charging case","price":299.99}]}
```
Action: `select_product{sku}`.

### `diagnostic_steps`
```jsonc
{"title":"Let's get the suction back","estimated_minutes":6,"symptom":"reduced_suction",
 "steps":[{"step_id":"s1","ord":1,"instruction":"Pop the dustbin out and check the filter for a grey felt of dust.",
           "image_url":"...","why":"A clogged filter is the cause about half the time.",
           "expected":"Filter looks clean or you cleaned it"}],
 "current_step":"s1"}
```
Action: `step_result{step_id, outcome: worked|failed|stuck}` — `failed` advances, `worked` closes the loop, `stuck` escalates.

### `order_card`
```jsonc
{"order_no":"ANK-2026-88213","channel":"official_store","purchase_date":"2026-03-14",
 "status":"delivered","items":[{"sku":"T2080","name":"eufy Omni S1 Pro","qty":1,"serial":"..."}],
 "warranty_until":"2028-03-14","source":"demo"}
```

### `warranty_result`
```jsonc
{"verdict":"needs_proof","reason_code":"DEALER_ORDER_NOT_IN_SYSTEM",
 "explanation":"That number looks like a dealer invoice, not a store order.",
 "required_evidence":["invoice_photo","dealer_name"],
 "next_action":"upload_proof","dealer":{"name":"PT Sinar Elektronik","region":"ID","service_path":"..."}}
```
Verdicts: `covered · covered_via_dealer · covered_pending_verification · needs_proof · not_covered_policy · expired · escalate_human`.

### `upload_request`
```jsonc
{"prompt":"Snap the sticker under the base — the serial starts with T.",
 "accepts":["image/jpeg","image/png"],"max_files":2,"purpose":"serial_verification"}
```

### `product_card` / `product_grid`
```jsonc
{"items":[{"sku":"T2080","name":"...","price":1499.99,"currency":"USD","image_url":"...",
           "url":"https://www.eufy.com/products/...","badges":["discontinued"],
           "reason":"Direct replacement for your S1 Pro"}]}
```

### `comparison_table`
```jsonc
{"skus":["T2080","T2081"],
 "rows":[{"label":"Suction","values":["8,000 Pa","10,000 Pa"]},
         {"label":"Price","values":["$1,499","$1,799"]}],
 "recommendation":{"sku":"T2081","why":"Same dock, stronger suction, still in production."}}
```

### `ticket_status`
```jsonc
{"ticket_id":"TCK-1042","status":"open","priority":"high","summary":"...",
 "timeline":[{"kind":"created","at":"2026-09-09T10:00:00Z","note":"Escalated: 2 failed steps + event tomorrow"}],
 "eta":"within 4 hours"}
```

### `human_handoff`
```jsonc
{"reason":"Two fixes failed and you have a deadline.","queue_position":3,"eta_minutes":8,
 "summary_preview":"eufy Omni S1 Pro, E-05, brush cleared, still errors. Bought 2026-03-14, in warranty.",
 "channels":["chat","email","phone"]}
```

### `form`
```jsonc
{"title":"Where should the replacement go?",
 "fields":[{"id":"name","label":"Full name","type":"text","required":true},
           {"id":"serial","label":"Serial number","type":"text","hint":"Under the base","required":true}],
 "submit_label":"Send it"}
```
Action: `submit{fields:{...}}`.

### `quick_replies`
```jsonc
{"items":[{"text":"It's still not working"},{"text":"How do I clean the brush?"},{"text":"Talk to a person"}]}
```

### `link_list` / `video_guide`
```jsonc
{"items":[{"title":"Omni S1 Pro user manual (PDF)","url":"...","kind":"manual","page":24}]}
```

---

## 5. Action → resume semantics

1. Frontend `POST /chat/action`.
2. Backend loads the LangGraph checkpoint for `session_id`, marks the block `answered`, injects the action as a tool observation, resumes at the node that paused.
3. A new SSE stream continues the same `message_id` chain (a fresh `message_id` for the assistant's follow-up).
4. Acting on an `expired` or already-`answered` block returns `409 {"code":"BLOCK_STALE"}`; the UI disables the block and asks the user to type instead.

---

## 6. Client rules (frontend must honour)

- Ignore unknown events and unknown block types; render a neutral fallback card rather than crashing.
- Track open stages; if `complete` arrives with stages still open, close them locally (defensive — the backend guarantees closure, but a dropped connection does not).
- Buffer `content_delta` and paint on `requestAnimationFrame`; do not re-render per token.
- Never re-post an action for a block whose `state !== "active"`.
- On `error{retryable:true}` show a retry affordance that re-sends the *same* turn, not a new one.

---

## 7. Environment variables

**Backend** (`.env`):
```
BACKEND_API_KEY=
RKAPI_BASE_URL=https://cdn.rkapi.com/v1
RKAPI_OPENAI_KEYS=            # comma-separated; each key is one parallel lane
RKAPI_MODEL=gpt-5.6-terra
GEMINI_EMBED_API_KEY=         # Google AI Studio, gemini-embedding-001
EMBED_MODEL=gemini-embedding-001
EMBED_DIM=3072
OPENAI_API_KEY=               # fallback embedder (text-embedding-3-large)
PINECONE_API_KEY=
PINECONE_INDEX=anker-support
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=    # REQUIRED for migrations/seed — not yet provided
SUPABASE_DB_URL=              # postgresql://... for LangGraph checkpointer + Alembic
TAVILY_API_KEY=               # optional, allowlisted web search
DEEPGRAM_API_KEY=             # pending
VOICE_ENABLED=false
CRAWL_USER_AGENT=AnkerHackathonBot/0.1 (+contact)
MAX_REACT_ITERATIONS=6
```

**Frontend** (`.env.local`):
```
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_SUPABASE_URL=https://wmxucgywnafvlnybkjhi.supabase.co
NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
BACKEND_API_KEY=              # server-side only, proxied through a route handler
```

The browser never holds `BACKEND_API_KEY`: `/chat` and `/chat/action` are proxied by a Next.js route handler that streams the SSE through unchanged.
