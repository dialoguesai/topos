#!/usr/bin/env bash
set -euo pipefail

if ! command -v ollama >/dev/null 2>&1; then
  echo "Missing ollama runtime. Install from https://ollama.com/download" >&2
  exit 1
fi

status=0
python - <<'PY' || status=$?
import importlib.util
import sys

missing = [m for m in ("transformers", "torch") if importlib.util.find_spec(m) is None]
if missing:
    print("Missing Python engine deps: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(2)
PY
status=$?
if [[ "${status}" == "2" ]]; then
  echo "Installing colocated engine deps (uv sync --extra engine)..."
  uv sync --extra engine
  python - <<'PY'
import importlib.util
import sys

missing = [m for m in ("transformers", "torch") if importlib.util.find_spec(m) is None]
if missing:
    print("Engine deps still missing after sync: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
PY
fi

export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export ENGINE_OLLAMA_BASE_URL="${ENGINE_OLLAMA_BASE_URL:-http://${OLLAMA_HOST}}"
export TOPOS_ENGINE_ENABLE_OLLAMA_RUNTIME=1

ollama serve >/tmp/topos-local-ollama.log 2>&1 &
OLLAMA_PID=$!

cleanup() {
  if kill -0 "${OLLAMA_PID}" >/dev/null 2>&1; then
    kill "${OLLAMA_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 20); do
  if curl -fsS "http://${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

exec uv run uvicorn topos.app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-9000}" --log-config topos/config/uvicorn_logging.json
