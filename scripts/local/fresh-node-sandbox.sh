#!/usr/bin/env bash
# Run a *fresh-install* topos-node next to your real one, without touching
# ~/.topos.
#
# SCOPE: this covers the TERMINAL install path only. It cannot test the macOS
# .app: the Swift shell resolves the node through Foundation's
# `homeDirectoryForCurrentUser`, which reads directory services and ignores
# $HOME (verified), so no env override redirects a GUI launch. See
# docs/testing/MANUAL_INSTALL_TEST.md — the app path needs a second macOS user
# account.
#
# Why HOME and not TOPOS_DATABASE_PATH: the node only parameterizes the SQLite
# file. The raw-file store, logs, ~/.topos/.env, the macOS Application Support
# dir and the legacy ~/.topos_engine probe are all `Path.home() / ...` literals
# (see topos/storage/raw/file_store.py, topos/core/logging.py,
# topos/cli/commands.py, topos/storage/db/paths.py). Overriding HOME is the only
# switch that moves all of them at once, so the sandbox cannot reach your data
# even if a new code path adds another hardcoded home-relative path.
#
#   scripts/local/fresh-node-sandbox.sh                  # install + run on :9100
#   scripts/local/fresh-node-sandbox.sh --name second    # a second sandbox
#   scripts/local/fresh-node-sandbox.sh --version 1.3.3  # a specific PyPI release
#   scripts/local/fresh-node-sandbox.sh --local          # install the working tree
#   scripts/local/fresh-node-sandbox.sh --reset          # wipe the sandbox first
#   scripts/local/fresh-node-sandbox.sh --shell          # env-only, drop to a shell
set -euo pipefail

REAL_HOME="${HOME}"
NAME="default"
PORT="9100"
HOST="127.0.0.1"
VERSION=""
INSTALL_LOCAL=0
RESET=0
SHELL_ONLY=0
LINK_MESSAGES=0
ROOT_BASE="${TOPOS_SANDBOX_BASE:-${REAL_HOME}/.topos-sandboxes}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) NAME="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --local) INSTALL_LOCAL=1; shift ;;
        --reset) RESET=1; shift ;;
        --shell) SHELL_ONLY=1; shift ;;
        --link-messages) LINK_MESSAGES=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

ROOT="${ROOT_BASE}/${NAME}"
SBHOME="${ROOT}/home"

if [[ "${RESET}" == "1" && -d "${ROOT}" ]]; then
    # Refuse to delete anything that is not under the sandbox base, and never
    # the real home — this script's whole job is to keep those apart.
    case "${ROOT}" in
        "${ROOT_BASE}"/?*) ;;
        *) echo "refusing to reset ${ROOT}: not under ${ROOT_BASE}" >&2; exit 1 ;;
    esac
    echo "reset: removing ${ROOT}"
    rm -rf "${ROOT}"
fi

mkdir -p "${SBHOME}"

# Caches stay in the real home. Models are multi-GB and read-only to the node;
# re-downloading torch + sentence-transformers per sandbox is the difference
# between a 20-second start and a 20-minute one.
export HOME="${SBHOME}"
export HF_HOME="${REAL_HOME}/.cache/huggingface"
export TORCH_HOME="${REAL_HOME}/.cache/torch"
export UV_CACHE_DIR="${REAL_HOME}/.cache/uv"

# macOS ingestion readers look at $HOME/Library/Messages and
# $HOME/Library/Application Support/Signal. Off by default: a fresh install
# should see an empty machine. Opt in when testing the ingest path itself.
if [[ "${LINK_MESSAGES}" == "1" ]]; then
    mkdir -p "${SBHOME}/Library/Application Support"
    ln -sfn "${REAL_HOME}/Library/Messages" "${SBHOME}/Library/Messages"
    ln -sfn "${REAL_HOME}/Library/Application Support/Signal" \
        "${SBHOME}/Library/Application Support/Signal"
fi

VENV="${ROOT}/venv"
if [[ ! -x "${VENV}/bin/topos-node" ]]; then
    echo "installing topos-node into ${VENV}"
    uv venv "${VENV}" >/dev/null
    if [[ "${INSTALL_LOCAL}" == "1" ]]; then
        REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
        VIRTUAL_ENV="${VENV}" uv pip install --python "${VENV}/bin/python" \
            "${REPO_ROOT}[local]"
    elif [[ -n "${VERSION}" ]]; then
        VIRTUAL_ENV="${VENV}" uv pip install --python "${VENV}/bin/python" \
            "topos-node[local]==${VERSION}"
    else
        VIRTUAL_ENV="${VENV}" uv pip install --python "${VENV}/bin/python" \
            "topos-node[local]"
    fi
fi
export PATH="${VENV}/bin:${PATH}"

# Unset anything inherited that would punch through the sandbox.
unset TOPOS_DATABASE_PATH TOPOS_INGESTION_BASE_PATH TOPOS_LOG_FILE TOPOS_KEY

# The sync dataset is keyed `{TOPOS_USER_ID}:{TOPOS_DEFAULT_DATASET_ID}`
# (topos/app.py). Two nodes signed into the same account would otherwise share
# one dataset namespace on the control plane; name this one after the sandbox.
export TOPOS_DEFAULT_DATASET_ID="sandbox-${NAME}"

cat <<EOF

  sandbox      ${NAME}
  HOME         ${SBHOME}
  database     ${SBHOME}/.topos/database.db  (created on first run)
  raw files    ${SBHOME}/.topos/ingestion
  env file     ${SBHOME}/.topos/.env
  binary       $(command -v topos-node)
  version      $("${VENV}/bin/python" -c 'import importlib.metadata as m; print(m.version("topos-node"))' 2>/dev/null || echo unknown)
  listening    http://${HOST}:${PORT}
  dataset      ${TOPOS_DEFAULT_DATASET_ID}

  your real ~/.topos is untouched.

  The react-app reaches this node through the control plane over the node's
  outbound sync socket, not over this port — so the deployed app at
  topos.dialogues.ai works as-is, no local frontend and no port juggling.
  All it needs is a TOPOS_KEY of its own: create a second self-hosted Topos in
  the app, copy the key from the Terminal tab, then in another window run

    HOME=${SBHOME} PATH=${VENV}/bin:\$PATH topos-node --set-topos-key <KEY>

  and restart this process. The setup panel flips to Connected on that key's
  fleet row, and you can chat against that Topos specifically.

EOF

if [[ "${SHELL_ONLY}" == "1" ]]; then
    echo "dropping into a sandboxed shell (exit to leave)"
    exec "${SHELL:-/bin/bash}"
fi

exec topos-node --host "${HOST}" --port "${PORT}" --no-tray
