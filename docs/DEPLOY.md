Written for: Bryan, running these commands himself.

# Deploying to a permanent link — free

Everything in this repo is ready to deploy. Nothing has been deployed: that needs your
accounts, and publishing is yours to authorise. This is what is prepared, what I need from
you, and the exact commands.

## The shape

```
  judges' browser
        │
        ▼
  Vercel  ──── Next.js frontend, free, always warm
        │      (route handlers keep BACKEND_API_KEY server-side — unchanged)
        ▼
  Cloud Run ── FastAPI backend, free tier, scales to zero
        │
        ├── Supabase Postgres   (already hosted)
        └── Pinecone            (already hosted)
```

The database and the vector index are already remote, so only two things need a home.

## Why these two

| | why | catch |
|---|---|---|
| **Vercel** (frontend) | Next.js 16 is theirs; free tier has no sleep, no card | none that matters here |
| **Cloud Run** (backend) | 2M requests + 180k vCPU-seconds free per month; scales to zero | **card required** to enable billing, and a cold start |

**Render's free tier is the one to avoid.** It sleeps after 15 minutes idle and takes ~50
seconds to wake. A judge clicking your link during judging gets a spinner. Cloud Run's cold
start is ~5 seconds — the app reaches "startup complete" in 1.4s locally, plus container
boot.

**If you will not add a card anywhere**, use Hugging Face Spaces (Docker) for the backend
instead — genuinely free, no card. It sleeps after prolonged inactivity and is slower, but
it works, and the same `Dockerfile` deploys there unchanged.

## What I need from you

1. **Which backend host** — Cloud Run (card on file, faster) or HF Spaces (no card, slower).
2. **A GCP project id**, if Cloud Run. Create one at console.cloud.google.com, enable
   billing, then tell me the id.
3. **Confirmation to deploy.** I will not push anything outward without you saying so.

That is all. The secrets are already in your `.env`; they go in as deploy-time variables
and are never written into the image — `.dockerignore` excludes `.env`, because an image
is a tarball anyone with registry access can unpack, and a key baked into a layer stays
there even if a later layer deletes it.

## The commands

### Backend — Cloud Run

Cloud Build compiles the image remotely, so you do not need Docker installed (you don't
have it).

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com

cd anker-hackathon-backend
gcloud run deploy anker-care-agent \
  --source . \
  --region asia-southeast1 \
  --allow-unauthenticated \
  --memory 1Gi \
  --timeout 300 \
  --set-env-vars "$(grep -v '^#' .env | grep -v '^$' | paste -sd, -)"
```

`--timeout 300` matters: the agent streams, and some turns run to ~40 seconds. The default
would be fine today but leaves no headroom.

`asia-southeast1` is Singapore — closest to you and to the judges. Latency to Supabase and
Pinecone depends on where those live; if they are in `us-east`, `us-central1` is the better
choice and worth a test.

It prints a URL like `https://anker-care-agent-xxxx.a.run.app`.

### Frontend — Vercel

```bash
cd anker-hackathon-frontend
npx vercel --prod
```

It will ask for the environment variables. Copy them from `.env.local`, setting
`NEXT_PUBLIC_API_URL` to the Cloud Run URL above:

```
NEXT_PUBLIC_API_URL=https://anker-care-agent-xxxx.a.run.app
BACKEND_API_KEY=<same value as local>
NEXT_PUBLIC_SUPABASE_URL=<same>
NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=<same>
```

`BACKEND_API_KEY` must NOT gain a `NEXT_PUBLIC_` prefix — that prefix is precisely what
puts a value into the browser bundle. It is read only in `app/api/*/route.ts`, which runs
on Vercel's server, and keeping it there is the whole point of the proxy.

### CORS — you probably do not need it

Worth knowing so you don't chase it: **the browser never calls the backend.** Every
reference to `NEXT_PUBLIC_API_URL` lives inside `app/api/*/route.ts` — server-side route
handlers — so the call to Cloud Run is server-to-server from Vercel, and CORS is not
enforced on it.

Set it anyway if you want to hit the backend from a browser tab while debugging:

```bash
gcloud run services update anker-care-agent \
  --region asia-southeast1 \
  --update-env-vars CORS_ORIGINS=https://your-app.vercel.app
```

(Minor, not worth changing now: `NEXT_PUBLIC_API_URL` does not need its prefix, since
nothing client-side reads it. The prefix only publishes the backend URL into the browser
bundle. Harmless — the URL is not a secret and `BACKEND_API_KEY` still never leaves the
server — but the name implies a client dependency that does not exist.)

## Verifying it

```bash
curl https://YOUR-BACKEND-URL/healthz
```

`/healthz` pings the LLM, embeddings, Pinecone and Postgres for real and reports each one,
so it tells you which dependency is misconfigured rather than just that something is. Use
`/livez` for anything automated — it answers from the process without calling upstream.

## What is already done

- `Dockerfile` — two-stage, non-root, slim (not alpine: musl has no manylinux wheels, so
  every dependency would compile from source and the build would take ~10 minutes instead
  of ~1).
- `.dockerignore` — excludes `.env`, `.secrets/`, the venv, and local scratch.
- `run.py` reads `HOST` and `PORT` from the environment, defaulting to the dev values. A
  container that binds `127.0.0.1` is unreachable from outside itself.
- The container health check hits `/livez`, not `/healthz`. Polling the deep check every
  30 seconds would spend ~2,880 embedding requests a day proving the app is alive, against
  the same single free-tier key the corpus is embedded with — and this project has already
  had a Google account banned over request volume.

**Not verified: the image has never been built.** There is no Docker on this machine, so
the Dockerfile is written but untested. The first `gcloud run deploy` is also the first
build; if it fails it will fail in Cloud Build with a readable error, and the likely
candidates are a missing system library for `psycopg` or `pillow`, both of which are
already installed in the runtime stage.
