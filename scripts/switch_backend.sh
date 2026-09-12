#!/usr/bin/env bash
# Flip the stack between cloud and local without touching code.
#   ./scripts/switch_backend.sh cloud            # Pinecone + Gemini + Supabase pooler
#   ./scripts/switch_backend.sh local            # sqlite-vec + local/OSS embedder
#   ./scripts/switch_backend.sh local --emb http://127.0.0.1:11434/v1 --model bge-m3
#   ./scripts/switch_backend.sh status
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=.env
set_kv() { # key value
  if grep -qE "^$1=" "$ENV"; then
    python3 - "$1" "$2" <<'PY'
import re, sys, pathlib
k, v = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env"); t = p.read_text()
p.write_text(re.sub(rf"^{k}=.*$", f"{k}={v}", t, flags=re.M))
PY
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV"
  fi
}
case "${1:-status}" in
  cloud)
    set_kv VECTOR_BACKEND pinecone
    set_kv EMBED_PROVIDER gemini
    echo "backend: CLOUD  (Pinecone + Gemini embeddings; DATABASE_URL untouched -> Supabase)"
    ;;
  local)
    shift || true
    set_kv VECTOR_BACKEND sqlite
    set_kv EMBED_PROVIDER openai_compat
    while [ $# -gt 0 ]; do
      case "$1" in
        --emb) set_kv EMBED_BASE_URL "$2"; shift 2 ;;
        --model) set_kv EMBED_LOCAL_MODEL "$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    echo "backend: LOCAL  (sqlite-vec + OpenAI-compatible embedder)"
    echo "  set DATABASE_URL to a local Postgres if you also want the DB offline"
    ;;
  status)
    grep -E "^(VECTOR_BACKEND|EMBED_PROVIDER|EMBED_BASE_URL|EMBED_LOCAL_MODEL|EMBED_DIM|DATABASE_URL)=" "$ENV" | sed 's/\(KEY=\).*/\1<redacted>/'
    ;;
  *) echo "usage: $0 {cloud|local|status}"; exit 2 ;;
esac
