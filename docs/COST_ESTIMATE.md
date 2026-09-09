# Cost Estimate — Anker Care Agent

> Built from **measured** prices and **measured** token counts, not vendor marketing.
> Every price below was either read off the RKAPI billing catalog or probed live on 2026-09-09.
> Two currencies appear throughout, and confusing them is the classic mistake:
>
> - **credit-USD** — what RKAPI's console reports. This is the number the API returns.
> - **real-USD** — what the money actually costs. Measured ratio on a prior project's RKAPI console: `real ≈ credit × 0.0266` (credit figures are ~37.6× inflated). Treat real-USD as an estimate until this account's own console confirms the ratio; treat credit-USD as authoritative.

---

## 1. Unit prices

| Service | Model | Price | Currency | Source |
|---|---|---|---|---|
| RKAPI (openai group) | `gpt-5.6-terra` | base **$2 / $12** per M in/out, group multiplier **×1.5** → **$3 / $18** per M | credit-USD | billing catalog, re-verified 2026-08-12 |
| RKAPI cached input | `gpt-5.6-terra` | **0.1×** the input rate | credit-USD | same |
| Google AI Studio | `gemini-embedding-001` | **$0.15 / M** input tokens | **real-USD** | Google list price |
| Pinecone serverless | `anker-support`, 3072-d | ~$0.33 / GB-month storage; reads/writes metered | real-USD | our volume sits inside the free tier |
| Supabase | free tier | 500 MB DB, 1 GB storage, 5 GB egress | real-USD | $0 at this scale |
| Tavily | web search | 1,000 free credits / month | real-USD | $0 |
| Deepgram | `nova-3` | ~$0.0043 / min prerecorded, ~$0.0077 / min streaming | real-USD | $200 free credit on signup |

**Measured constraint:** the RKAPI openai-group key reaches `gpt-5.6-terra` **and nothing else**. `gpt-5.4-mini` returns *"not supported when using Codex with a ChatGPT account"*; `gpt-5.4-nano`, `gpt-4.1-nano`, `gpt-4o-mini`, `gemini-3-flash-preview` and `claude-haiku` all return `403 no access`. There is no cheap classifier tier on this key — see §5 for the lever that would create one.

---

## 2. Token budget per turn

Measured anchor: a real classify prompt on `gpt-5.6-terra` billed **61 in / 73 out**, so this model does *not* burn a large hidden reasoning budget on short structured tasks. The estimates below scale that up with realistic context.

### A typical troubleshooting turn (2–3 ReAct iterations, retrieval, one answer)

| Call | Input | Output |
|---|---|---|
| `perceive` (system 700 + history 900 + user 120) | 1,700 | 250 |
| `rewrite` | 900 | 80 |
| ReAct iterations × 2.5 (system 1,200 + tool schemas 1,600 + state 2,200 + observations 1,800) | 17,000 | 1,750 |
| rerank (30 candidates × ~120 tok) | 3,800 | 250 |
| `compose` (6 chunks × 800 + policy + history) | 6,200 | 950 |
| **Total** | **29,600** | **3,280** |

System prompts and tool schemas repeat identically on every call, so roughly **40 % of input is cacheable** at 0.1×:

```
effective input = 29,600 × (0.60 + 0.40 × 0.1) = 18,944 tokens
cost = 18,944 × $3/M  +  3,280 × $18/M
     = $0.0568        +  $0.0590        =  $0.116 credit  ≈  $0.0031 real
```

### Per-turn table

| Turn type | Effective in | Out | credit-USD | real-USD |
|---|---|---|---|---|
| Simple Q&A (no tools) | 3,840 | 1,100 | **$0.031** | $0.0008 |
| Troubleshooting (2–3 tools) | 18,944 | 3,280 | **$0.116** | $0.0031 |
| With a photo (+1,100 in per image) | 19,650 | 3,280 | **$0.118** | $0.0031 |
| Heavy (6 iterations, ceiling) | 38,000 | 6,500 | **$0.231** | $0.0061 |

**A 5-turn conversation ≈ $0.45 credit ≈ $0.012 real.** Vision is close to free at these sizes — an image is ~1,100 tokens, about 4 % of a turn.

---

## 3. Corpus build — one-off

| Item | Volume | credit-USD | real-USD |
|---|---|---|---|
| Embedding (products + manuals + KB + tickets + community) | ~4.6 M tokens @ $0.15/M | — | **$0.69** |
| VLM captions for product media | 1,800 images × (1,100 in + 120 out) | **$9.83** | $0.26 |
| Error-code + spec extraction from manuals | 200 docs × (8 k in + 1.5 k out) | **$10.20** | $0.27 |
| Crawling | bandwidth only | — | $0 |
| **Subtotal** | | **~$20** | **~$1.22** |

Re-crawls are near-free: chunks carry a `text_hash`, so a re-embed pays only for what changed.

---

## 4. Whole-project projection

| Phase | Turns | credit-USD | real-USD |
|---|---|---|---|
| Development iteration | ~1,500 | $174 | $4.63 |
| Eval runs (60 cases × 3 turns × 8 runs) | 1,440 | $167 | $4.44 |
| LLM judge (480 calls) | — | $10 | $0.27 |
| Load test (50 VU × 5 turns × 3 runs) | 750 | $87 | $2.31 |
| Demo day | 300 | $35 | $0.93 |
| Corpus build (§3) | — | $20 | $1.22 |
| Infra (Pinecone + Supabase + Tavily) | — | — | **$0** — free tiers |
| **Total** | **~4,000** | **≈ $493 credit** | **≈ $14 real** |

Deepgram, if the key arrives: 200 voice minutes for the demo ≈ **$1.54**, and the $200 signup credit covers it outright.

**Headline: the whole pilot lands around $500 in RKAPI credits and roughly $15 of real money.** The build is not cost-constrained; it is time-constrained.

---

## 5. Cost levers, in order of payoff

1. **Prompt caching is the whole game.** Keep the system prompt and tool schemas byte-identical across calls and put them first in the message list. Cached input bills at 0.1×. Sloppiness here (a timestamp in the system prompt, tools reordered per call) silently triples input cost. Assert cache-hit rate in the load test.
2. **Trim ReAct state.** Feed the loop a summarised observation, keep the full tool result in `tool_traces`. Observations are the fastest-growing part of the context and the least re-read.
3. **Skip the LLM when a rule answers.** Alias table, error-code lookup, order-number pattern match, and the warranty engine cost nothing. Every question they resolve is a turn that never reaches the model. This is also why they exist — cost is the second reason, correctness is the first.
4. **Rerank lexically first.** Run cosine + lexical scoring, and call the LLM reranker only when the top candidates are within a small margin. Removes ~3,800 input tokens from most turns.
5. **A cheap classifier lane — unverified but worth one test.** The RKAPI **claude-group** key (a different token from the openai-group one) reaches `claude-haiku-4-5-20251001` at $1/$5 per M with a ×1.0 multiplier — about 3× cheaper input and 3.6× cheaper output than terra. `perceive` and rerank are ~35 % of input volume and need no reasoning model. If that key is available to this project, moving those two calls saves roughly a fifth of total spend. One probe settles it; it has not been run for these accounts.
6. **Cap iterations at 6.** Typical is 2–3; the cap bounds the tail, and the heavy-turn row above is that bound priced out.

---

## 6. What would blow the estimate

| Risk | Effect | Guard |
|---|---|---|
| Cache misses from a non-static system prompt | input cost ×1.6 | assert hit rate in load test; no clocks or per-turn ids in the prefix |
| ReAct looping on an unanswerable question | up to the 6-iteration ceiling per turn | hard cap + a "say you don't know" exit at iteration 4 |
| Re-crawling and re-embedding the whole corpus | +$0.69 real, +an hour | `text_hash` skip already in the pipeline |
| Manual PDFs far larger than assumed | extraction cost scales linearly | measure after task 9, before embedding everything |
| Load test run repeatedly at 50 VU | $29 credit per full run | run the full 50-VU sweep twice, not twenty times |

---

## 7. Monitoring

Every model call writes `{model, key_index, input, output, cached, cost_credit}` to `tool_traces`, and the `usage` SSE event surfaces the per-turn number in the trace drawer. `GET /metrics` exposes running totals per session and per day, so the spend is visible during the build rather than discovered at the end.
