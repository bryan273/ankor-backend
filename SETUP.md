# Setup — getting this running on another machine

Two repos, two terminals, about five minutes. **You do not need to re-crawl anything**:
the product catalog, the support knowledge base and all 32k vectors already live in
shared Supabase and Pinecone projects, so a fresh clone talks to the same data this was
built against.

What you do need is the keys, and those are not in the repo (see [Credentials](#credentials)).

---

## 1. Backend

```bash
git clone https://github.com/tkc88888888/anker-hackathon-backend
cd anker-hackathon-backend

python -m venv .venv
# Windows:
.venv\Scripts\pip install -r requirements.txt
# macOS / Linux:
# .venv/bin/pip install -r requirements.txt

cp .env.example .env          # then paste in the keys — see below
python run.py                 # http://127.0.0.1:8000
```

Check it came up:

```bash
curl http://127.0.0.1:8000/healthz
```

You want `"status": "ok"` and all four dependencies green — `deepseek`, `embed`,
`pinecone`, `supabase`. If one is red, the `error` field on that entry says which key is
wrong.

> **Use `python run.py`, not `uvicorn app.main:app`.** On Windows, psycopg's async driver
> refuses to run on the default event loop, and `uvicorn.run()` resets the loop policy
> after import — so a policy set at import time is silently undone. `run.py` keeps the
> loop ours. The failure it prevents is not an exception: the connection pool just never
> opens, and thirty seconds later every query reports a closed pool.

## 2. Frontend

Second terminal:

```bash
git clone https://github.com/tkc88888888/anker-hackathon-frontend
cd anker-hackathon-frontend

npm install
cp .env.local.example .env.local     # paste the same BACKEND_API_KEY
npm run dev                          # http://localhost:3000
```

> **Use `127.0.0.1`, not `localhost`, in `NEXT_PUBLIC_API_URL`.** Node 18+ resolves
> `localhost` to `::1` while the backend binds IPv4, so the proxy fails with
> `ECONNREFUSED` against a server that is plainly running.

Open **http://localhost:3000**. You land on the storefront — browse the catalog, and
the chat launcher is the button in the bottom-right corner. The switch at the top right
changes which of the three views you are looking at; it is a demo control, not a login.

---

## Credentials

`.env` is gitignored, deliberately — the keys are live and one of them bills real money.
Get them from Bryan and paste them into your `.env`. Both `.env.example` files list every
variable with a comment explaining what it does.

The minimum to run:

| Variable | What breaks without it |
|---|---|
| `DEEPSEEK_API_KEY` | every text call — the agent cannot answer at all |
| `RKAPI_OPENAI_KEYS` | photo understanding (and the text fallback if DeepSeek is down) |
| `GEMINI_EMBED_API_KEY` | retrieval — the agent loses the knowledge base |
| `PINECONE_API_KEY` | same |
| `SUPABASE_REF` + `SUPABASE_DB_PASSWORD` | products, orders, dealers, tickets |
| `BACKEND_API_KEY` | any string, as long as both repos use the same one |

`TAVILY_API_KEY` and `DEEPGRAM_API_KEY` are optional; web search and voice stay switched
off without them and nothing else is affected.

---

## Try it

The landing page offers the four scenarios from the brief. The two worth watching:

**"my S1 Pro isn't sucking anymore"** — S1 Pro is both a robot vacuum and a wearable
breast pump, so the agent stops and asks which. Pick the breast pump and the answer
continues about duckbill valves, not brush rolls. Add "and the milk isn't coming out" to
the same sentence and it resolves silently without asking.

**"order SE-482911 isn't recognised"** — that invoice exists only in the dealer
directory, never in the order system. Watch the pipeline strip at the top of the answer:
it shows the intent it classified, the tools it looped through, and the warranty verdict
the rule engine returned.

Click any `[1]` in an answer to open the source it came from. Tap a pipeline step to see
what that step concluded.

---

## Tests

```bash
pytest -q                                   # 123 unit tests, no network, ~2 s
python scripts/eval_run.py                  # 22 scenario evals against a running server
python scripts/eval_run.py --scenario S2    # just the disambiguation cases
python scripts/smoke_resume.py              # the pause → click → resume round trip
python scripts/load_test.py --users 50      # concurrency
```

The eval suite and the load test **cost real credits** (roughly 0.02 per turn, so a full
eval run is about 0.5 credits). `pytest` costs nothing and needs no network.

---

## Rebuilding the data yourself

Only if you want your own Supabase/Pinecone rather than the shared ones. The crawl is
polite (~1 request/second) so the first run takes a while; everything is cached on disk
afterwards, and the embedder skips content whose hash has not changed.

```bash
python scripts/db_bootstrap.py --apply      # schema — idempotent
python scripts/crawl_products.py --limit 220
python scripts/crawl_support.py --limit 550
python scripts/seed_legacy_products.py      # discontinued products the crawler cannot see
python scripts/build_aliases.py             # asserts the "s1 pro" ambiguity exists
python scripts/seed_demo.py                 # asserts the S3 dealer fixture exists
python scripts/embed_corpus.py
python scripts/audit_vectors.py             # confirms every vector still maps to a row
```

Two of those scripts **assert rather than just build**. `build_aliases.py` fails if
`"s1 pro"` stops being ambiguous across two categories, and `seed_demo.py` fails if
`SE-482911` ever appears in the orders table. Both of those would quietly turn a headline
demo into an ordinary lookup, so they fail loudly instead.

---

## If something is wrong

| Symptom | Cause |
|---|---|
| Frontend shows `UPSTREAM_UNREACHABLE` | backend not running, or `NEXT_PUBLIC_API_URL` says `localhost` instead of `127.0.0.1` |
| Every DB call says "pool is already closed" | started with `uvicorn` instead of `python run.py` (Windows) |
| `403 error code: 1010` from RKAPI | Cloudflare rejecting a default User-Agent — only affects hand-rolled HTTP, the SDK path is fine |
| Answers arrive but cite nothing | `GEMINI_EMBED_API_KEY` or `PINECONE_API_KEY` is wrong; `/healthz` will show it |
| `429 BUSY` | working as designed above 60 concurrent turns; retry after 5 s |
| Port 8000 already in use | an earlier `run.py` is still alive — kill it before restarting |
| **Page renders but nothing is clickable** | the dev server is refusing its own HMR WebSocket, so React never hydrates — see below |

### The page that looks fine and is completely dead

Worth its own section, because everything about it says the app is working. The layout
renders, the styling is right, the text is correct — and no button, tab or switch does
anything. Nothing appears in the terminal, and `curl` reports a perfectly healthy page.

The cause is `allowedDevOrigins` in `next.config.ts`. Declaring that array (we need it
for ngrok) makes it the *entire* allowlist, and `next dev` origin-checks its HMR
WebSocket against it. With only the tunnel hosts listed, the browser at
`http://localhost:3000` had its own upgrade request refused, so hydration never ran and
the page was server-rendered HTML with no React attached.

`curl` cannot reproduce it: curl sends no `Origin` header, so it is never checked. To
see it directly, send one —

```bash
curl -i -H "Origin: http://127.0.0.1:3000" -H "Connection: Upgrade"      -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13"      -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="      "http://127.0.0.1:3000/_next/hmr?id=test"
```

`HTTP/1.1 101 Switching Protocols` is healthy. An empty response means the origin was
rejected: add `localhost`, `127.0.0.1` and `[::1]` to `allowedDevOrigins`. They are in
there now, so this should stay fixed — but if you add a host to that array, add it
*alongside* the local ones, never instead of them.
