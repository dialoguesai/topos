set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

# Public OSS-friendly local workflows for a colocated Topos node.
# These commands intentionally avoid project-internal deployment details.

default:
    @just --list

# Install colocated local runtime (database + engine, includes sentence-transformers).
setup:
    uv sync --extra local

# Install development toolchain + colocated runtime.
setup-dev:
    uv sync --extra dev --extra local

# Run the node (with tray). `just run dev` = DEBUG; optional host/port; extra args pass through.
run *args:
    #!/usr/bin/env bash
    # Goes through the `topos-node` CLI rather than uvicorn directly, so this gets
    # the menu-bar tray, key resolution and the shell contract — the CLI is where
    # all of that lives, and a bare `uvicorn topos.app:app` silently skips it.
    #
    # Still the working tree's code: `uv run` resolves to this project's venv, not
    # to the `uv tool install` copy on PATH (that one is a frozen snapshot).
    set -eu
    log_level=INFO
    host="0.0.0.0"
    port="9000"
    read -r -a rest <<< "{{args}}"
    idx=0
    if [[ ${#rest[@]} -gt 0 && "${rest[0]}" == "dev" ]]; then
        log_level=DEBUG
        idx=1
    fi
    if [[ ${#rest[@]} -gt idx ]]; then host="${rest[idx]}"; fi
    if [[ ${#rest[@]} -gt $((idx + 1)) ]]; then port="${rest[idx + 1]}"; fi
    extra=()
    if [[ ${#rest[@]} -gt $((idx + 2)) ]]; then extra=("${rest[@]:idx + 2}"); fi
    # --skip-update-check: running from source, so a PyPI version probe is only latency.
    # `${extra[@]+...}` guard: macOS ships bash 3.2, where expanding an empty
    # array under `set -u` is an unbound-variable error — i.e. plain `just run`
    # would fail, which is the most common way to call this.
    LOG_LEVEL="${log_level}" uv run topos-node \
        --host "${host}" --port "${port}" --skip-update-check \
        ${extra[@]+"${extra[@]}"}

# Bare uvicorn, no CLI and no tray — for debugging the ASGI app itself.
run-bare *args:
    #!/usr/bin/env bash
    set -eu
    log_level=INFO
    host="0.0.0.0"
    port="9000"
    read -r -a rest <<< "{{args}}"
    idx=0
    if [[ ${#rest[@]} -gt 0 && "${rest[0]}" == "dev" ]]; then
        log_level=DEBUG
        idx=1
    fi
    if [[ ${#rest[@]} -gt idx ]]; then host="${rest[idx]}"; fi
    if [[ ${#rest[@]} -gt $((idx + 1)) ]]; then port="${rest[idx + 1]}"; fi
    LOG_LEVEL="${log_level}" uv run uvicorn topos.app:app --host "${host}" --port "${port}" --log-config topos/config/uvicorn_logging.json

# Run colocated node with Ollama runtime and auto-install missing engine deps.
run-local host="127.0.0.1" port="9000":
    HOST="{{host}}" PORT="{{port}}" bash scripts/local/run-local-engine-with-ollama.sh

# Fresh-install a node in an isolated HOME (never touches ~/.topos). See
# scripts/local/fresh-node-sandbox.sh --help for --name/--version/--reset.
sandbox *args:
    bash scripts/local/fresh-node-sandbox.sh {{args}}

# Prepare/restore THIS account for a from-scratch install test (the .app path,
# which cannot be HOME-sandboxed). `status` | `prepare --yes` | `restore --yes`.
app-install-test *args="status":
    bash scripts/local/app-install-test.sh {{args}}

# Run test suites.
test:
    uv run pytest tests -m "public and not e2e and not live and not qq_eval" -q

# Per-release privacy evaluation → version-stamped scorecard in eval_reports/<version>.json
# (+ history.jsonl trend). Exits non-zero if a tier-1 privacy gate regresses. Run right after
# the version bump so the report is stamped with the version being shipped.
eval-release:
    uv run python scripts/run_release_eval.py --print

# Release gate: everything ci.yml checks, runnable locally before tagging a
# release (dep pins in sync, migration checksums, public test lane incl. the
# handled-message-types protocol snapshot guards, build + release smoke).
gate:
    uv run python scripts/sync-dep-pins.py --check
    uv run python scripts/sync_migration_checksums.py --check
    uv run pytest tests -m "public and not e2e and not live and not qq_eval" -q
    uv build
    uv run python scripts/release_smoke_test.py

# Discover local databases and exit.
discover:
    uv run python -m topos.cli --discover
