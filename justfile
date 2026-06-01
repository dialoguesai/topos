set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

# Public OSS-friendly local workflows for a colocated Topos node.
# These commands intentionally avoid project-internal deployment details.

default:
    @just --list

# Install default local runtime (database + engine colocated).
setup:
    uv sync --extra engine

# Install development toolchain + colocated runtime.
setup-dev:
    uv sync --extra dev --extra engine

# Run API directly.
run host="0.0.0.0" port="9000":
    uv run uvicorn topos.app:app --host {{host}} --port {{port}}

# Run colocated node with Ollama runtime and auto-install missing engine deps.
run-local host="127.0.0.1" port="9000":
    HOST="{{host}}" PORT="{{port}}" bash scripts/local/run-local-engine-with-ollama.sh

# Run test suites.
test:
    uv run pytest tests -m "public and not e2e" -q

# Discover local databases and exit.
discover:
    uv run python -m topos.cli --discover
