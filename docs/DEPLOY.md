Written for: Bryan, running these commands himself.

# Deploying to a permanent link, free

Everything in this repo is ready to deploy. Nothing has been deployed: that needs your
accounts, and publishing is yours to authorise. This is what is prepared, what I need from
you, and the exact commands.

## The shape

```
  judges' browser
        │
        ▼
  Vercel  ──── Next.js frontend, free, always warm
        │      (route handlers keep BACKEND_API_KEY server-side, unchanged)
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
start is ~5 seconds, because the app reaches "startup complete" in 1.4s locally, plus container
boot.

**Hugging Face Spaces no longer works for this, and that was tested rather than assumed.**
An earlier version of this document recommended it as the no-card option. Creating the
Space now fails:

```
402 Payment Required
Static Spaces are free for everyone, but hosting Gradio and Docker Spaces
on free cpu-basic requires a PRO subscription.
```

Public and private both refuse, so the only way through HF is PRO at $9 a month. The
Dockerfile itself is fine there; the tier is the problem.

**The no-card option is now Render.** 750 free instance hours a month, which covers one
service running continuously, Docker deploys supported, and no card until you exceed the
quota. The catch is real and worth planning around: a free service spins down after 15
minutes without traffic and takes about a minute to come back. Open the link a few minutes
before judging, or ping it every ten minutes during the session.

## You do not need GitHub access for any of this

Worth saying first, because it is the thing that looked like a blocker. Neither host
below reads your GitHub repository.

- **Vercel** has two paths. Connecting a repo installs a GitHub App, and installing one
  needs admin rights on `tkc88888888/anker-hackathon-frontend`. The CLI does not: it
  uploads the folder on your disk. That is the command written below, and it has never
  needed GitHub. What you give up is redeploy-on-push, which for a hackathon is a command
  you run rather than a feature you need.
- **Hugging Face Spaces** hosts its own git at `huggingface.co`. You push to that remote.
  GitHub is not involved at any point.

If you ever do want the GitHub link, you do not need anyone to change your role: fork the
repo to your own account (forking needs only read access) and connect the fork, which you
own outright. Failing that, the org owner does not need to promote you either, they only
need to install the Vercel app on the repo once.

## What I need from you

1. **Which backend host**: Cloud Run (card required, wakes in ~5s), Render (no card, wakes
   in ~60s), or HF Spaces (PRO at $9/month, wakes in ~30s).
2. **A GCP project id**, if Cloud Run. Create one at console.cloud.google.com, enable
   billing, then tell me the id.
3. **Confirmation to deploy.** I will not push anything outward without you saying so.

That is all. The secrets are already in your `.env`; they go in as deploy-time variables
and are never written into the image. `.dockerignore` excludes `.env`, because an image
is a tarball anyone with registry access can unpack, and a key baked into a layer stays
there even if a later layer deletes it.

## The commands

### Backend: Cloud Run

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

`asia-southeast1` is Singapore, the closest region to you and to the judges. Latency to Supabase and
Pinecone depends on where those live; if they are in `us-east`, `us-central1` is the better
choice and worth a test.

It prints a URL like `https://anker-care-agent-xxxx.a.run.app`.

### Backend: Hugging Face Spaces (no card)

The same `Dockerfile` deploys unchanged. `run.py` already reads `HOST` and `PORT` from the
environment and the image already sets `0.0.0.0:8080` and a non-root user, which is what
Spaces asks for.

**One file has to change.** A Space is configured by YAML front matter at the top of the
repository's `README.md`, so add this as the very first lines of `README.md`:

```
---
title: Ankor Care Agent
emoji: 🔌
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 8080
pinned: false
---
```

It costs a small block of YAML at the top of the GitHub README. That is the whole price.

Then create the Space at huggingface.co/new-space (SDK: **Docker**, hardware: **CPU basic**,
free) and push to it:

```bash
cd anker-hackathon-backend
git remote add space https://huggingface.co/spaces/YOUR_USERNAME/ankor-care-agent
git push space development:main
```

Secrets go in the Space's **Settings → Variables and secrets**, never in the image. The
ones that matter:

| | |
|---|---|
| required | `DATABASE_URL` or the `SUPABASE_*` set, `DEEPSEEK_API_KEY`, `PINECONE_API_KEY`, `PINECONE_HOST`, `PINECONE_INDEX`, `GEMINI_EMBED_API_KEY`, `BACKEND_API_KEY` |
| optional | `TAVILY_API_KEY` (web search), `RKAPI_OPENAI_KEYS` (failover provider), `CORS_ORIGINS` |
| not needed in production | `OPENAI_API_KEY`, which is only read by the eval judge, and that runs on your machine |

Copy the names from your `.env`; the values never leave your machine except into that
settings page.

The Space serves at `https://YOUR_USERNAME-ankor-care-agent.hf.space`. Check it with
`curl https://.../livez` before wiring the frontend to it.

**The catch, honestly:** a free Space sleeps after a long idle period and takes tens of
seconds to wake. Open the link once before judging starts and it stays warm through the
session.

### Frontend: Vercel

```bash
cd anker-hackathon-frontend
npx vercel --prod
```

It will ask for the environment variables. The frontend reads exactly two, and a grep for
`process.env` across the whole app confirms it:

```
NEXT_PUBLIC_API_URL=<the backend URL from above>
BACKEND_API_KEY=<same value as local>
```

`.env.local` also carries `NEXT_PUBLIC_SUPABASE_URL` and
`NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`, and an earlier version of this document told you to
set them on Vercel. Nothing reads them: the browser never talks to Supabase, the backend
owns that connection. Leave them out.

`BACKEND_API_KEY` must NOT gain a `NEXT_PUBLIC_` prefix. That prefix is precisely what
puts a value into the browser bundle. It is read only in `app/api/*/route.ts`, which runs
on Vercel's server, and keeping it there is the whole point of the proxy.

### CORS, which you probably do not need

Worth knowing so you don't chase it: **the browser never calls the backend.** Every
reference to `NEXT_PUBLIC_API_URL` lives inside `app/api/*/route.ts`, which are server-side
route handlers, so the call to the backend is server-to-server from Vercel and CORS is not
enforced on it.

Set it anyway if you want to hit the backend from a browser tab while debugging:

```bash
gcloud run services update anker-care-agent \
  --region asia-southeast1 \
  --update-env-vars CORS_ORIGINS=https://your-app.vercel.app
```

(Minor, not worth changing now: `NEXT_PUBLIC_API_URL` does not need its prefix, since
nothing client-side reads it. The prefix only publishes the backend URL into the browser
bundle. It is harmless, since the URL is not a secret and `BACKEND_API_KEY` still never
leaves the server, but the name implies a client dependency that does not exist.)

## Verifying it

```bash
curl https://YOUR-BACKEND-URL/healthz
```

`/healthz` pings the LLM, embeddings, Pinecone and Postgres for real and reports each one,
so it tells you which dependency is misconfigured rather than just that something is. Use
`/livez` for anything automated: it answers from the process without calling upstream.

## What is already done

- `Dockerfile`, two-stage, non-root, slim (not alpine: musl has no manylinux wheels, so
  every dependency would compile from source and the build would take ~10 minutes instead
  of ~1).
- `.dockerignore` excludes `.env`, `.secrets/`, the venv, and local scratch.
- `run.py` reads `HOST` and `PORT` from the environment, defaulting to the dev values. A
  container that binds `127.0.0.1` is unreachable from outside itself.
- The container health check hits `/livez`, not `/healthz`. Polling the deep check every
  30 seconds would spend ~2,880 embedding requests a day proving the app is alive, against
  the same single free-tier key the corpus is embedded with, and this project has already
  had a Google account banned over request volume.

**Not verified: the image has never been built.** There is no Docker on this machine, so
the Dockerfile is written but untested. The first `gcloud run deploy` is also the first
build; if it fails it will fail in Cloud Build with a readable error, and the likely
candidates are a missing system library for `psycopg` or `pillow`, both of which are
already installed in the runtime stage.
