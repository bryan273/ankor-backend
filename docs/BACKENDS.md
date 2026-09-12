# Backends: cloud ⇄ local, switchable by configuration

An adapter layer (`app/adapters/`) sits behind the two seams the codebase already had,
so **any build on this foundation switches stacks without code changes**:

| Concern | Seam (unchanged for callers) | Cloud default | Local option |
|---|---|---|---|
| Vectors | `app/clients/vectors.get_vectors()` | Pinecone `anker-support` (3072-d) | **sqlite-vec**, one file on disk |
| Embeddings | `app/clients/embed.get_embedder()` | Gemini `gemini-embedding-001` | **any OpenAI-shaped server** (Ollama / llama.cpp / vLLM / MLX) |
| Relational | `app/clients/db` via `DATABASE_URL` | Supabase pooler | local Postgres (`.localdb`, same schema) |
| Chat model | `app/clients/llm.get_llm()` | DeepSeek / RKAPI | OpenRouter free tier, or any OpenAI-compatible endpoint (already exercised) |

Defaults reproduce the hosted stack exactly, so nothing changes until a flag is set.

## Switching

```bash
./scripts/switch_backend.sh status     # what is live right now
./scripts/switch_backend.sh cloud      # Pinecone + Gemini
./scripts/switch_backend.sh local --emb http://127.0.0.1:11434/v1 --model bge-m3
```

| Env | Values | Meaning |
|---|---|---|
| `VECTOR_BACKEND` | `pinecone` (default) · `sqlite` | where vectors live |
| `EMBED_PROVIDER` | `gemini` (default) · `openai_compat` · `hash` | who makes vectors |
| `EMBED_BASE_URL` / `EMBED_LOCAL_MODEL` | e.g. `http://127.0.0.1:11434/v1` / `bge-m3` | local embedder endpoint |
| `VECTOR_SQLITE_PATH` | `.localvec/anki_support.db` | local index file |
| `EMBED_DIM` | `3072` | must match the chosen model + store |
| `DATABASE_URL` | pooler or local DSN | relational source of truth |

## Building the local index

```bash
# copy vectors straight out of Pinecone — no embedding calls, no quota, byte-identical
python scripts/local_stack_up.py --mode copy  --namespace products,kb --limit 5000

# or re-embed the text locally (fully self-hosted, no Google)
EMBED_PROVIDER=openai_compat EMBED_BASE_URL=http://127.0.0.1:11434/v1 EMBED_LOCAL_MODEL=bge-m3 \
  python scripts/local_stack_up.py --mode embed --namespace kb --limit 200
```

## Acceptance gate

An adapter is only safe if the answer does not change:

```bash
python scripts/parity_check.py --n 200 --top-k 10     # exit 0 = parity holds
```

It seeds the same vectors into both backends (throwaway Pinecone namespace + a scratch
local namespace), runs identical queries and reports `overlap@k`. Default embedder is
`hash` — deterministic and quota-free — so parity runs offline; point `EMBED_PROVIDER`
at a real model to check semantics too.

## Adding a backend

Implement the duck-type and register it in the factory — callers never change:

- vector store: `upsert` · `query` · `delete_namespace` · `stats` · `aclose`
- embedder: `embed_one` · `embed_many` · `embed_query` · `aclose`

Candidates already costed: **pgvector** (best fit when Postgres is local), **Qdrant**
(built-in quantization), **LanceDB** (embedded/columnar), **Chroma**, **FAISS**;
lexical fallbacks that need no vectors at all: Postgres FTS, **ParadeDB `pg_search`**,
SQLite FTS5, Typesense, Meilisearch.

## Sizing note

The text corpus is tiny (**~75 MB** for all ~19,400 scrapable pages); raw HTML is the
fat (**~8.1 GB**) and should be extracted-and-discarded. Vectors decide the rest:
float32 ≈ 9.1 GB, float16 ≈ 4.6 GB, int8 ≈ 2.3 GB for ~494k chunks. On a laptop with
little headroom, either keep vectors in Pinecone and store only text locally, or use
`VECTOR_BACKEND=sqlite` with a lower `EMBED_DIM` (Matryoshka models such as
`nomic-embed-text-v1.5` / `bge-m3` truncate cleanly to 1024).
