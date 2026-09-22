#!/usr/bin/env zsh

# Source this file before running Python, Docker, imports, tests, or the API.
# All intentional runtime data is kept inside this external-SSD checkout.

if [[ -n "${ZSH_VERSION:-}" ]]; then
  SCRIPT_SOURCE="${(%):-%N}"
elif [[ -n "${BASH_SOURCE:-}" ]]; then
  SCRIPT_SOURCE="${BASH_SOURCE[0]}"
else
  SCRIPT_SOURCE="$0"
fi

PROJECT_ROOT="$(cd "$(dirname "$SCRIPT_SOURCE")/.." && pwd -P)"
# The external-SSD root is the default because it keeps multi-gigabyte model
# caches and the Postgres data directory off the internal disk. A fresh checkout
# on another machine has no such SSD, and refusing to run there would make the
# project unreproducible, so the gate is an explicit opt-out rather than an
# absolute rule. The default behaviour is unchanged.
if [[ "$PROJECT_ROOT" != /Volumes/MAC/* && "${ALLOW_NON_SSD_RUNTIME:-0}" != "1" ]]; then
  print -u2 "Refusing to configure runtime outside the external SSD: $PROJECT_ROOT"
  print -u2 "Re-run with ALLOW_NON_SSD_RUNTIME=1 to keep the runtime inside this checkout."
  return 1 2>/dev/null || exit 1
fi

export TALENT_ATLAS_PROJECT_ROOT="$PROJECT_ROOT"
export RUNTIME_ROOT="$PROJECT_ROOT/.runtime"
# The copied source .env may still reference a hosted database. This runtime is
# deliberately self-contained, so sourced shells always target local Docker.
export DATABASE_URL="${LOCAL_DATABASE_URL:-postgresql://hybrid_user:hybrid_pass@127.0.0.1:5432/hiring_platform}"
export SEARCH_DATABASE_URL="${LOCAL_SEARCH_DATABASE_URL:-}"
export LANGFUSE_ENABLED="${LOCAL_LANGFUSE_ENABLED:-false}"
export OTEL_ENABLED="${LOCAL_OTEL_ENABLED:-false}"
export HF_HOME="$RUNTIME_ROOT/cache/huggingface"
export TRANSFORMERS_CACHE="$RUNTIME_ROOT/cache/transformers"
export SENTENCE_TRANSFORMERS_HOME="$RUNTIME_ROOT/cache/sentence-transformers"
export TORCH_HOME="$RUNTIME_ROOT/cache/torch"
export PIP_CACHE_DIR="$RUNTIME_ROOT/cache/pip"
export UV_CACHE_DIR="$RUNTIME_ROOT/cache/uv"
export XDG_CACHE_HOME="$RUNTIME_ROOT/cache/xdg"
export TMPDIR="$RUNTIME_ROOT/tmp"

mkdir -p \
  "$RUNTIME_ROOT/postgres" \
  "$RUNTIME_ROOT/cache/huggingface" \
  "$RUNTIME_ROOT/cache/transformers" \
  "$RUNTIME_ROOT/cache/sentence-transformers" \
  "$RUNTIME_ROOT/cache/torch" \
  "$RUNTIME_ROOT/cache/pip" \
  "$RUNTIME_ROOT/cache/uv" \
  "$RUNTIME_ROOT/cache/xdg" \
  "$RUNTIME_ROOT/tmp" \
  "$RUNTIME_ROOT/logs" \
  "$RUNTIME_ROOT/traces" \
  "$RUNTIME_ROOT/recordings"

print "Talent Atlas external runtime"
print "  project: $PROJECT_ROOT"
print "  runtime: $RUNTIME_ROOT"
print "  temp:    $TMPDIR"
print "  models:  $HF_HOME"
print "  database: local Docker PostgreSQL"
print "  remote telemetry: disabled"
