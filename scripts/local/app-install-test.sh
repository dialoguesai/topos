#!/usr/bin/env bash
# Prepare THIS user account to test a from-scratch Topos install, then put it
# back. For the macOS .app path, which cannot be sandboxed with HOME (the Swift
# shell resolves the node through Foundation's `homeDirectoryForCurrentUser`,
# which reads directory services and ignores $HOME).
#
#   app-install-test.sh status               what would shadow a fresh install
#   app-install-test.sh prepare --yes        move it all aside
#   app-install-test.sh restore --yes        put it back
#
# Everything is MOVED, never copied and never deleted. Source and destination
# are on the same filesystem, so each move is an atomic rename: your 480MB
# database is not duplicated and is never rewritten. `restore` moves anything
# the test created aside (keeping it for inspection) before restoring.
#
# Fidelity ceiling: this gives you the app's real bootstrap branch on a clean
# home. It does NOT give you a clean Gatekeeper first-run or an uncontested
# `topos:` LaunchServices claim — those are per-user state that survives this.
# For those, use a second macOS user account (see docs/testing/MANUAL_INSTALL_TEST.md).
set -euo pipefail

STASH_BASE="${TOPOS_STASH_BASE:-${HOME}/.topos-install-test}"
MANIFEST="${STASH_BASE}/manifest.txt"

# Everything the node or the shell probes on this account.
TARGETS=(
    "${HOME}/.topos"
    "${HOME}/.topos_engine"
    "${HOME}/.local/bin/topos-node"
    "${HOME}/Library/Application Support/ToposControlPlane"
    "${HOME}/Library/Application Support/ToposEngine"
)

CMD="${1:-status}"
CONFIRM=0
for a in "${@:2}"; do [[ "$a" == "--yes" ]] && CONFIRM=1; done

_slug() { printf '%s' "$1" | sed "s|^${HOME}/||; s|/|__|g; s| |_|g"; }

_assert_node_stopped() {
    # Test who actually holds the files being moved, not "is any topos-node
    # running" — a HOME-sandboxed node from `just sandbox` is unrelated to these
    # paths and must not block a stash.
    local holders=""
    # `lsof <dir>` does NOT report processes holding files INSIDE that dir, so
    # checking the target directories is not enough — verified: it happily
    # cleared a stash while a process had the database open. Check the actual
    # database files, then any node/shell process with a file open under the
    # stash targets.
    local f
    for f in "${HOME}"/.topos/database.db "${HOME}"/.topos/database.db-wal \
             "${HOME}"/.topos_engine/database.db \
             "${HOME}/Library/Application Support/ToposEngine/database.db"; do
        [[ -e "$f" ]] || continue
        local h
        h="$(lsof -t -- "$f" 2>/dev/null | tr '\n' ' ' || true)"
        [[ -n "${h// /}" ]] && holders+="${f} (pids: ${h})"$'\n'
    done
    local pid
    for pid in $(pgrep -f 'topos-node|ToposShell' 2>/dev/null || true); do
        if lsof -p "${pid}" 2>/dev/null | grep -qF "${HOME}/.topos"; then
            holders+="pid ${pid} has files open under ${HOME}/.topos"$'\n'
        fi
    done
    if [[ -n "${holders}" ]]; then
        echo "refusing: these paths are open by a live process —" >&2
        printf '%s' "${holders}" >&2
        echo "stop that process first; renaming a directory out from under an open SQLite WAL risks the database." >&2
        exit 1
    fi
}

case "${CMD}" in
status)
    echo "Would shadow a fresh install on this account:"
    for t in "${TARGETS[@]}"; do
        if [[ -e "$t" ]]; then
            printf '  PRESENT  %s\n' "$t"
        else
            printf '  absent   %s\n' "$t"
        fi
    done
    echo
    if [[ -f "${MANIFEST}" ]]; then
        echo "A prepare is currently ACTIVE — stashed at ${STASH_BASE}:"
        sed 's/^/  /' "${MANIFEST}"
        echo
        echo "Run: app-install-test.sh restore --yes"
    else
        echo "No stash active. Run: app-install-test.sh prepare --yes"
    fi
    echo
    echo "Not handled here (per-user state a stash cannot fake):"
    echo "  - Gatekeeper first-run: this Mac has already assessed the bundle."
    echo "  - 'topos:' URL scheme claims currently registered:"
    /System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister \
        -dump 2>/dev/null | grep -c 'claimed schemes:.*topos:' | sed 's/^/      /' || echo "      (could not read)"
    echo "  - /Applications/Topos.app is shared by every account on this Mac."
    ;;

prepare)
    if [[ -f "${MANIFEST}" ]]; then
        echo "a prepare is already active (${MANIFEST}). restore first." >&2
        exit 1
    fi
    if [[ "${CONFIRM}" != "1" ]]; then
        echo "This MOVES the following aside (atomic rename, nothing copied or deleted):"
        for t in "${TARGETS[@]}"; do [[ -e "$t" ]] && printf '  %s\n' "$t"; done
        echo
        echo "Re-run with --yes to proceed."
        exit 0
    fi
    _assert_node_stopped
    mkdir -p "${STASH_BASE}"
    : > "${MANIFEST}"
    for t in "${TARGETS[@]}"; do
        [[ -e "$t" ]] || continue
        dest="${STASH_BASE}/$(_slug "$t")"
        if [[ -e "${dest}" ]]; then
            echo "refusing: stash slot already occupied: ${dest}" >&2
            exit 1
        fi
        mv "$t" "${dest}"
        printf '%s\t%s\n' "${dest}" "$t" >> "${MANIFEST}"
        echo "stashed  $t"
    done
    echo
    echo "This account now looks like a machine that has never run Topos."
    echo "Download the DMG from the website (do NOT reuse /Applications/Topos.app —"
    echo "it may be a local build ahead of what users can download), install, and launch."
    echo
    echo "Put everything back with: app-install-test.sh restore --yes"
    ;;

restore)
    if [[ ! -f "${MANIFEST}" ]]; then
        echo "no active stash at ${MANIFEST}" >&2
        exit 1
    fi
    if [[ "${CONFIRM}" != "1" ]]; then
        echo "Would restore:"
        sed 's/^/  /' "${MANIFEST}"
        echo
        echo "Anything the test created at those paths is moved to a .test-run-* sibling, not deleted."
        echo "Re-run with --yes to proceed."
        exit 0
    fi
    _assert_node_stopped
    while IFS=$'\t' read -r dest orig; do
        [[ -n "${dest}" ]] || continue
        if [[ -e "${orig}" ]]; then
            # The install test created its own copy here. Keep it — it is the
            # artifact you just spent the test producing.
            keep="${STASH_BASE}/test-run__$(_slug "$orig")"
            rm -rf "${keep}"
            mv "${orig}" "${keep}"
            echo "test artifact kept at ${keep}"
        fi
        mkdir -p "$(dirname "${orig}")"
        mv "${dest}" "${orig}"
        echo "restored ${orig}"
    done < "${MANIFEST}"
    rm -f "${MANIFEST}"
    echo
    echo "Restored. Verify before trusting it:"
    echo "  ls -la ~/.topos/database.db"
    echo "  topos-node --discover   # note: --discover does not list ~/.topos (known gap)"
    ;;

*)
    sed -n '2,12p' "$0"
    exit 2
    ;;
esac
