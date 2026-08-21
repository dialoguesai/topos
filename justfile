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

# Run test suites. TWO pytest sessions, on purpose — see test-privacy-battery.
test:
    uv run pytest tests -m "public and not e2e and not live and not qq_eval" -q
    just test-privacy-battery

# The privacy firewall battery: UAR/CER zero-leak probes, the black-hole leak
# tests, minimality, the negotiation ratchet, dense sparsification, redaction
# idempotence and the release-eval gates. Hermetic and deterministic — no live
# LLM, no owner database, ~40s for 305 tests.
#
# Reached by PATH, not by marker, because everything under tests/evals/privacy
# is auto-marked `private` (tests/conftest.py PRIVATE_PATH_HINTS) and `just
# test`'s `-m public and ...` therefore deselects all of it. `private` means
# "not part of the lane an OSS fork's CI runs" — a packaging statement. It was
# being read as a statement about importance, and these are the most productive
# tests in the repo: they caught the black-hole existence leak, the SG1
# access-mode ceiling gap, and both of Q7's roster leaks.
#
# Until 2026-08-20 only ci.yml reached them (its "Privacy firewall + eval gates"
# step, which this mirrors), so `just test` and `just gate` were both green on a
# machine where the privacy plane was red — you found out on push, if at all.
#
# A second invocation rather than re-marking the battery `public`: `public`
# decides what the OSS lane runs, so flipping it would drag 305 internal-fixture
# tests into that lane as a side effect. Naming the path costs one line and
# changes nothing else.
test-privacy-battery *args:
    uv run pytest tests/evals/privacy -q {{args}}

# The owner-database query/latency eval, against a SNAPSHOT of ~/.topos.
#
# These cases are only meaningful on real data, so they are not seeded — but
# every turn they run persists a query_artifacts row, and pointed at the live
# file that write-back lands in the owner's database (2026-08-19: 97% of that
# table, and 100% of its timed rows, were harness sessions, so
# `just latency-percentiles` was reporting the test suite's latency as the
# owner's). The snapshot keeps the data and throws away the writes.
#
# Takes a ~488MB online backup first; that is the price of a lane that cannot
# corrupt or pollute the thing it measures.
# --ignore, because `live` currently spans two different needs: these modules
# resolve the database from TOPOS_DATABASE_PATH and so follow the snapshot,
# while tests/release/iteration4 talks HTTP to the node on :9000 and would
# ignore it — see `just test-live-node`.
test-owner-db-eval *args:
    #!/usr/bin/env bash
    set -euo pipefail
    snap="$(uv run python scripts/snapshot_owner_db.py)"
    trap 'rm -f "$snap" "$snap"-wal "$snap"-shm' EXIT
    echo "snapshot: $snap"
    TOPOS_DATABASE_PATH="$snap" uv run pytest tests -m "qq_eval or live" \
        --ignore=tests/release/iteration4 -q {{args}}

# Live-node lane: drives the engine actually running on :9000 with a real key.
#
# Deselected from every other lane on purpose. It is environment-dependent by
# construction — a 500 here is a statement about this machine's node, not about
# the code under test — and it reads and writes whichever database that node
# has open, which no env var in THIS process can redirect.
test-live-node *args:
    uv run pytest tests/release/iteration4 -m "live" -q {{args}}

# Per-release privacy evaluation → version-stamped scorecard in eval_reports/<version>.json
# (+ history.jsonl trend). Exits non-zero if a tier-1 privacy gate regresses. Run right after
# the version bump so the report is stamped with the version being shipped.
eval-release:
    uv run python scripts/preflight_release_env.py
    uv run python scripts/run_release_eval.py --print

# Release gate: everything ci.yml checks, runnable locally before tagging a
# release (dep pins in sync, migration checksums, public test lane incl. the
# handled-message-types protocol snapshot guards, the privacy firewall battery,
# build + release smoke).
#
# EVERY leg runs under the owner-database tripwire. The pytest legs get it from
# tests/conftest.py; the others get it from scripts/live_db_tripwire.py, which
# arms the SAME tests/live_db_watch module in the leg and in every Python child
# it spawns — including one in another virtualenv running another Python, which
# is what the release smoke test is.
#
# It is wired here because the hermeticity work of 2026-08-19/20 stopped at the
# pytest boundary and leg 7 is one step past it: release_smoke_test.py booted the
# built wheel against $HOME/.topos/database.db (source=slot, profile=personaldb,
# schema=62) and ran CREATE TABLE IF NOT EXISTS on it. The pre-release ritual was
# writing to the owner's Topos. Worse, when the tripwire was first armed at that
# leg the connect was refused and the leg STILL exited 0 — core.state catches the
# failure and the app serves /healthcheck degraded — so the tripwire fails the leg
# from its own journal rather than from the leg's exit code.
gate:
    uv run python scripts/live_db_tripwire.py scripts/preflight_release_env.py
    uv run python scripts/live_db_tripwire.py scripts/sync-dep-pins.py --check
    uv run python scripts/live_db_tripwire.py scripts/sync_migration_checksums.py --check
    uv run pytest tests -m "public and not e2e and not live and not qq_eval" -q
    just test-privacy-battery
    uv run python scripts/live_db_tripwire.py --command uv build
    uv run python scripts/live_db_tripwire.py scripts/release_smoke_test.py

# Home chat Wave A retrieval-quality gates. LOCAL ONLY: these drive
# demo/signal_dimension_harness, whose fixture data cannot ship in this repo
# (`demo/` is gitignored), so CI cannot run them — see the note in
# .github/workflows/ci.yml. Seeds a throwaway DB.
#
# "never touches ~/.topos" was NOT true until 2026-08-21: evaluate_harness.py
# scored brief quality over HTTP against --engine-url, default
# http://127.0.0.1:9000 — the operator's live node on their real data. Every
# other number came from the seeded DB, so the gate went red as real life
# drifted and printed the owner's brief text into /tmp/query-catalog.out. It now
# reads the briefs the seed writes, out of the same DB. One live-DB read
# remains: the evaluator's "Explorer tables" count goes through
# topos.core.state, which opens and MIGRATES ~/.topos/database.db. It is
# reported, never asserted on, and its printed label says so.
#
# THE BAR. Permission and Signal are hard 70/70: a permission miss is a
# disclosure bug and a signal miss means a retrieval layer went dark, and
# neither depends on how derived the database is. Answer `quality` is a
# RATCHET, not 70/70. The recorded 70/70 baseline
# (QUERY_CATALOG_latest-ingest-20260621.json) was measured on a fully derived
# node — real ingest, enrichment and signal derivation, so vector search and
# topic clusters were populated. This gate seeds a throwaway DB with zero
# embeddings and zero clusters, and `_bundle_is_global_db`
# (topos/query/retrieval.py) deliberately disables the vector and cluster
# layers for any non-global database, so a large share of the catalog cannot
# be answered here by construction. QUALITY_FLOOR is therefore the
# high-water mark on THIS environment: raise it whenever a change moves it up,
# never lower it to make a red gate pass.
#   34/70  2026-08-08  first honest measurement
#   42/70  2026-08-09  restored profile_records to public_bio:read and
#                      work_context:read (P-01..P-04, W-03, N-03, X-04)
#   45/70  2026-08-21  measured, not engineered: the run that fixed the
#                      evaluator's brief check scored 45 and the recipe asked
#                      for the lock-in. Which change earned the three points is
#                      NOT identified — nothing in that fix touches the catalog
#                      lane — so if this floor ever reads as a claim about the
#                      brief fix, it is not one.
QUALITY_FLOOR := "45"

harness-gate:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! -d demo/signal_dimension_harness ]]; then
        echo "demo/signal_dimension_harness is absent (it is gitignored, not shipped)." >&2
        echo "This gate only runs on a checkout that has the harness fixtures." >&2
        exit 1
    fi
    HARN_DB="$(mktemp /tmp/harness-gate.XXXXXX)"
    trap 'rm -f "${HARN_DB}" "${HARN_DB}"-wal "${HARN_DB}"-shm' EXIT
    uv run python demo/signal_dimension_harness/seed_ci_harness_db.py --db "${HARN_DB}"
    uv run python demo/signal_dimension_harness/run_multi_turn_catalog.py --db "${HARN_DB}"
    # The runner exits non-zero whenever any row fails, which under `pipefail`
    # would end the recipe before the floor is ever consulted. Swallow it: the
    # score below is the arbiter, and a crash still fails the gate because it
    # prints no score line for the extraction to find.
    uv run python demo/signal_dimension_harness/run_query_catalog.py \
        --catalog demo/signal_dimension_harness/QUERY_CATALOG.md --db "${HARN_DB}" \
        | tee /tmp/query-catalog.out || true
    grep -q "Permission: 70/70" /tmp/query-catalog.out
    grep -q "Signal: 70/70" /tmp/query-catalog.out
    QUALITY="$(sed -n 's/.*quality \([0-9]\{1,\}\)\/70.*/\1/p' /tmp/query-catalog.out | head -1)"
    if [[ -z "${QUALITY}" ]]; then
        echo "harness-gate: could not read the quality score from the catalog run." >&2
        exit 1
    fi
    if (( QUALITY < {{QUALITY_FLOOR}} )); then
        echo "harness-gate: quality ${QUALITY}/70 is below the floor of {{QUALITY_FLOOR}}/70." >&2
        exit 1
    fi
    if (( QUALITY > {{QUALITY_FLOOR}} )); then
        echo "harness-gate: quality ${QUALITY}/70 beats the floor of {{QUALITY_FLOOR}}/70 — raise QUALITY_FLOOR in the justfile to lock the gain in." >&2
    fi
    uv run python demo/signal_dimension_harness/evaluate_harness.py --db "${HARN_DB}"

# Discover local databases and exit.
discover:
    uv run python -m topos.cli --discover
