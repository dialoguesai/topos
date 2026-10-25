"""`topos-node status [--json]` and `topos-node doctor [--json]` (CONTRACTS §4).

Both read the node over its owner socket and the registry read-only; neither
makes a TCP connection or a network call (the `outdated` flag is the node's
own cached PyPI check). Exit codes are the contract's: 0 ok, 3 not paired,
4 node down, 5 rejected key; `doctor` adds 2 when it has findings.

The invariant the tests pin: no secret, no HTTP response body and no owner
content reaches stdout, stderr or the log. The commands print a projection
of known fields, never a body — the fake node in the tests plants a secret in
every body it serves to prove it.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import click

from topos import mcp_server
from topos.mcp_server import NodeDown, NodeRefused, NodeSocket
from topos.runtime_platform import INSTALL_PYTHON

EXIT_OK = 0
EXIT_NEEDS_HUMAN = 2
EXIT_NOT_PAIRED = 3
EXIT_NODE_DOWN = 4
EXIT_REJECTED = 5

CP_STATES = ("connected", "rejected", "degraded", "offline", "unknown")

#: Binary resolution, the Topos.app order (CONTRACTS §4).
BINARY_CANDIDATES = (
    "$TOPOS_NODE_BIN",
    "~/.local/bin/topos-node",
    "~/.topos/runtime/bin/topos-node",
    "/opt/homebrew/bin/topos-node",
    "/usr/local/bin/topos-node",
)

_ENV_LINE_RE = re.compile(r"^(TOPOS_KEY|TOPOS_OWNER_KEY)=(.*)$", re.M)

_JSON_OPTION = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")


# ---------------------------------------------------------------- local reads
def _env_file() -> Path:
    from topos.owner_key import _env_path

    return _env_path()


def _env_file_values() -> dict[str, str]:
    """The two key lines of ~/.topos/.env, for comparisons only — never printed."""
    try:
        text = _env_file().read_text(encoding="utf-8")
    except OSError:
        return {}
    return {m.group(1): m.group(2).strip().strip('"').strip("'") for m in _ENV_LINE_RE.finditer(text)}


def _local_version() -> str:
    from topos.runtime_update import get_runtime_version

    return get_runtime_version()


def _database_path() -> Optional[Path]:
    try:
        from topos.storage.db.paths import resolve_active_database

        return resolve_active_database().path
    except Exception:  # noqa: BLE001 — an unconfigured machine still gets a status
        return None


def _read_only(path: Path) -> Optional[sqlite3.Connection]:
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _engine_config_value(key: str) -> Optional[str]:
    path = _database_path()
    conn = _read_only(path) if path else None
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT value FROM engine_config WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    value = str(row[0]).strip() if row and row[0] is not None else ""
    return value or None


def _registry_agents() -> list[dict]:
    from topos import mcp_clients

    lister = getattr(mcp_clients, "list_agents", None)
    path = _database_path()
    conn = _read_only(path) if (lister and path) else None
    if conn is None:
        return []
    try:
        rows = lister(conn)
    except Exception:  # noqa: BLE001 — a status never fails on the registry
        return []
    finally:
        conn.close()
    return [
        {
            "client": str(r.get("client_id") or "")[len("agent-"):],
            "enrolled_at": r.get("created_at"),
            "revoked": bool(r.get("revoked_at")),
        }
        for r in rows
        if str(r.get("client_id") or "").startswith("agent-")
    ]


def _pairing_block() -> Optional[dict]:
    from topos.cli.agents_cmd import read_pairing_record

    record = read_pairing_record()
    if not record:
        return None
    return {"state": str(record.get("state") or "unknown"), "user_code": record.get("user_code")}


#: Lane L7's `/v1/shell/status` ``supervisor`` words → the status schema's three.
_SUPERVISOR_WORDS = {
    "topos-app": "topos_app", "topos_app": "topos_app",
    "launchd": "service", "systemd": "service", "service": "service",
    "headless": "manual", "terminal": "manual", "manual": "manual",
}


def _supervisor(shell: dict, pid: Optional[int]) -> Optional[str]:
    """The node's own word when it says one, else the process tree."""
    word = _SUPERVISOR_WORDS.get(str(shell.get("supervisor") or "").strip().lower())
    return word or _supervisor_for_pid(pid)


def _resolve_binary() -> Optional[str]:
    for candidate in BINARY_CANDIDATES:
        if candidate.startswith("$"):
            value = os.environ.get(candidate[1:])
            if value and Path(value).is_file():
                return value
            continue
        path = Path(os.path.expanduser(candidate))
        if path.is_file():
            return str(path)
    return None


# -------------------------------------------------------------- supervisor
def _process_parent(pid: int) -> Optional[tuple[int, str]]:
    """``(ppid, command)`` of one process from `ps`, or None when it is gone."""
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not out:
        return None
    parts = out.split(None, 1)
    try:
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    except ValueError:
        return None


def _supervisor_for_pid(pid: Optional[int]) -> Optional[str]:
    """Who keeps the node alive: Topos.app, a LaunchAgent/user unit, or a shell."""
    if not pid:
        return None
    chain: list[tuple[int, str]] = []
    current = int(pid)
    for _ in range(32):
        parent = _process_parent(current)
        if parent is None:
            break
        chain.append(parent)
        ppid = parent[0]
        if ppid <= 1 or ppid == current:
            break
        current = ppid
    if not chain:
        return None
    for _, command in chain:
        lowered = command.lower()
        if "toposshell" in lowered or "topos.app" in lowered:
            return "topos_app"
    if chain[0][0] == 1:
        return "service"
    return "manual"


# ------------------------------------------------------------------ status
def _owner_scope_sync(sock: NodeSocket) -> Optional[dict]:
    try:
        body = sock.request_sync("GET", mcp_server.OWNER_SCOPE_PATH)
    except (NodeDown, NodeRefused):
        return None
    return body if body.get("topos_id") else None


def _sources_sync(sock: NodeSocket, scope: Optional[dict], sightings: Optional[dict] = None) -> list[dict]:
    """The §4 source rows; ``sightings`` (when given) collects each source's secret_sightings count by id."""
    sightings = {} if sightings is None else sightings
    if not scope:
        return []
    ident = {k: scope.get(k) for k in ("user_id", "topos_id", "dataset_id")}
    try:
        result = mcp_server.dispatch_sync(sock, "get_sources", ident)
    except (NodeDown, NodeRefused):
        return []
    out = []
    for source in result.get("sources") or []:
        if not isinstance(source, dict) or not source.get("source_id"):
            continue
        sid = str(source["source_id"])
        try:
            settings = sock.request_sync("GET", f"/sources/{sid}/settings", params={"dataset_id": ident["dataset_id"]})
        except (NodeDown, NodeRefused):
            settings = {}
        if settings.get("status") == "error":
            settings = {}
        out.append(
            {
                "source_id": sid,
                "enabled": bool(settings.get("enabled", True)),
                "paused": bool(settings.get("paused", False)),
                "last_sync_at": settings.get("last_sync_at"),
                "last_error": settings.get("last_error"),
            }
        )
        # Counts only (L4's ledger): session files in which a node secret was scrubbed at the last sync. Beside
        # the row, not in it: `status --json` rows are the §4 shape; `doctor` reads the total.
        sightings[sid] = _secret_sightings(settings)
    return out


def _secret_sightings(settings: dict) -> int:
    """Lane L4's ledger counter — how many session FILES held a Topos secret at
    the last sync — wherever the settings row carries it. Counts only; the
    scrubbed values never leave the reader."""
    candidates = [
        settings.get("secret_sightings"),
        (settings.get("ledger") or {}).get("secret_sightings") if isinstance(settings.get("ledger"), dict) else None,
        (settings.get("last_sync_ledger") or {}).get("secret_sightings")
        if isinstance(settings.get("last_sync_ledger"), dict) else None,
        ((settings.get("last_sync_result") or {}).get("ledger") or {}).get("secret_sightings")
        if isinstance(settings.get("last_sync_result"), dict)
        and isinstance((settings.get("last_sync_result") or {}).get("ledger"), dict) else None,
    ]
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    return 0


def _secret_sightings_total(sightings: dict) -> int:
    """Sum of the sources' ``secret_sightings`` as the status pass already fetched them (one pass, not two)."""
    return sum(int(n or 0) for n in sightings.values())


def collect_status() -> dict:
    """The `status --json` document (CONTRACTS §4), exit code included."""
    return _collect()[0]


def _collect() -> tuple:
    """``(the status document, secret_sightings by source id)``: one pass over the node for both commands."""
    sightings: dict = {}
    socket_path = mcp_server.NodeSocket().socket_path
    socket_present = socket_path.exists()
    sock = NodeSocket(socket_path, timeout_s=15.0)
    shell: Optional[dict] = None
    try:
        shell = sock.request_sync("GET", mcp_server.SHELL_STATUS_PATH)
    except NodeDown:
        shell = None
    except NodeRefused:
        shell = {}  # it answered, so it runs; the rest stays unknown
    running = shell is not None
    shell = shell or {}

    scope = _owner_scope_sync(sock) if running else None
    sources = _sources_sync(sock, scope, sightings) if running else []
    pid = shell.get("pid") if running else None
    pid = int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None

    if running:
        topos_id = (scope.get("topos_id") if scope else None) or (str(shell.get("topos_id")).strip() if shell.get("topos_id") else None)
        user_id_present = bool(shell.get("user_id_present")) if "user_id_present" in shell else bool(scope and scope.get("user_id"))
    else:
        topos_id = _engine_config_value("topos_id")
        user_id_present = _engine_config_value("user_id") is not None
    paired = bool(topos_id)

    cp = str(shell.get("control_plane") or "") if running else ""
    control_plane = cp if cp in CP_STATES else "unknown"
    update = shell.get("update") if isinstance(shell.get("update"), dict) else {}
    outdated = bool(update.get("available")) if running else False
    latest = str(update.get("latest")) if outdated and update.get("latest") else None
    if running and "owner_key_persisted" in shell:
        owner_key_persisted = bool(shell.get("owner_key_persisted"))
    else:
        owner_key_persisted = bool(_env_file_values().get("TOPOS_OWNER_KEY"))

    if not running:
        exit_code = EXIT_NODE_DOWN
    elif control_plane == "rejected":
        exit_code = EXIT_REJECTED
    elif not paired:
        exit_code = EXIT_NOT_PAIRED
    else:
        exit_code = EXIT_OK

    return {
        "node": {
            "running": running,
            "version": str(shell.get("version") or _local_version()),
            "pid": pid,
            "socket": socket_present,
            "owner_key_persisted": owner_key_persisted,
            "supervisor": _supervisor(shell, pid) if running else None,
        },
        "paired": paired,
        "topos_id": str(topos_id) if topos_id else None,
        "user_id_present": user_id_present,
        "control_plane": control_plane,
        "outdated": outdated,
        "latest_version": latest,
        "agents": _registry_agents(),
        "sources": sources,
        "pairing": _pairing_block(),
        "exit_code": exit_code,
    }, sightings


def _status_lines(snapshot: dict) -> list[str]:
    node = snapshot["node"]
    lines = []
    if node["running"]:
        who = {"topos_app": "Topos.app", "service": "a background service", "manual": "a terminal"}.get(
            node["supervisor"] or "", "unknown"
        )
        lines.append(f"Topos {node['version']} is running (pid {node['pid']}, kept alive by {who}).")
    else:
        lines.append(f"Topos is not running (this binary is {node['version']}).")
    lines.append(
        "Paired with Topos " + (snapshot["topos_id"] or "") if snapshot["paired"] else "Not paired yet."
    )
    lines.append(f"Control plane: {snapshot['control_plane']}.")
    if snapshot["outdated"]:
        lines.append(f"A newer node is published: {snapshot['latest_version']}.")
    agents = snapshot["agents"]
    lines.append(
        "Agents: " + (", ".join(a["client"] + (" (disconnected)" if a["revoked"] else "") for a in agents) or "none")
        + "."
    )
    for source in snapshot["sources"]:
        state = "paused" if source["paused"] else ("enabled" if source["enabled"] else "off")
        lines.append(f"Source {source['source_id']}: {state}; last sync {source['last_sync_at'] or 'never'}.")
    if snapshot["pairing"]:
        lines.append(f"Pairing: {snapshot['pairing']['state']}.")
    return lines


@click.command("status")
@_JSON_OPTION
def status_command(as_json: bool) -> None:
    """Is Topos running, paired and connected? Exit 0/3/4/5 says which."""
    snapshot = collect_status()
    if as_json:
        click.echo(json.dumps(snapshot, indent=2, sort_keys=True))
    else:
        click.echo("\n".join(_status_lines(snapshot)))
    sys.exit(int(snapshot["exit_code"]))


# ------------------------------------------------------------------ doctor
def doctor_findings(snapshot: dict, *, secret_sightings: int = 0) -> list[dict]:
    """One finding per problem, each with the fix (CONTRACTS §4, PLAN §2.2)."""
    findings: list[dict] = []
    node = snapshot["node"]

    def add(code: str, message: str, fix: str) -> None:
        findings.append({"code": code, "message": message, "fix": fix})

    if secret_sightings > 0:
        files = "file" if secret_sightings == 1 else "files"
        add(
            "secrets_in_session_files",
            f"A Topos secret appears in {secret_sightings} of your coding-tool session {files} "
            "(scrubbed before anything reached your Topos).",
            "Rotate the key (`topos-node pair` issues a new one) and delete or edit those session files; "
            "the scrub keeps the old value out of your Topos either way.",
        )

    if _resolve_binary() is None:
        add(
            "binary_not_found",
            "No topos-node binary in the places the app and the plugin look.",
            f"Install the node: `uv tool install --python {INSTALL_PYTHON} topos-node` (or open Topos.app), "
            "then check `which topos-node`.",
        )
    if not node["running"]:
        add("node_down", "Topos is not running.", "Start it: `topos-node start --headless` (or open Topos.app).")
    if not node["socket"]:
        add(
            "socket_missing",
            "The owner socket (~/.topos/engine.sock) is missing.",
            "Starting Topos creates it; if Topos is running, check TOPOS_UDS_ENABLED is not set to off.",
        )
    if snapshot["control_plane"] == "rejected":
        add("key_rejected", "The control plane rejects this node's key.", "Pair again: `topos-node pair`.")
    if not snapshot["paired"]:
        add("not_paired", "This computer is not paired with a Topos.", "Run `topos-node pair`.")
    if snapshot["outdated"]:
        add(
            "outdated",
            f"A newer node is published ({snapshot['latest_version']}).",
            "Run `topos-node upgrade`.",
        )
    if not node["owner_key_persisted"]:
        add(
            "owner_key_not_persisted",
            "The owner key is not saved in ~/.topos/.env, so the node runs in legacy mode.",
            "Make ~/.topos writable by you (`chmod 700 ~/.topos`), then restart Topos so it can write the key.",
        )
    exported = (os.environ.get("TOPOS_KEY") or "").strip()
    stored = _env_file_values().get("TOPOS_KEY", "")
    if exported and stored and exported != stored:
        add(
            "exported_key_differs",
            "A TOPOS_KEY exported in this shell differs from the one in ~/.topos/.env; the file wins and the exported value is ignored.",
            "Run `unset TOPOS_KEY` and remove it from your shell profile so the two stop disagreeing; Topos reads ~/.topos/.env.",
        )
    active = {a["client"] for a in snapshot["agents"] if not a["revoked"]}
    for client in sorted(active):
        if not mcp_server.label_path(client).is_file():
            add(
                "label_missing",
                f"{mcp_server.DISPLAY_NAMES.get(client, client)} is enrolled but its label file is missing.",
                f"Run `topos-node agents add {client}`.",
            )
    for client in mcp_server.AGENT_NAMES:
        if client not in active and mcp_server.label_path(client).is_file():
            add(
                "label_orphan",
                f"A label file exists for {mcp_server.DISPLAY_NAMES.get(client, client)} but it is not enrolled.",
                f"Run `topos-node agents disconnect {client}`, then `topos-node agents add {client}`.",
            )
    return findings


@click.command("doctor")
@_JSON_OPTION
def doctor_command(as_json: bool) -> None:
    """Findings with their fixes; exit 2 when there is something to do."""
    snapshot, sightings = _collect()
    findings = doctor_findings(snapshot, secret_sightings=_secret_sightings_total(sightings))
    if as_json:
        click.echo(json.dumps({"findings": findings, "status": snapshot}, indent=2, sort_keys=True))
    elif not findings:
        click.echo("Nothing to fix.")
    else:
        for finding in findings:
            click.echo(f"- {finding['message']}\n  fix: {finding['fix']}")
    sys.exit(EXIT_NEEDS_HUMAN if findings else EXIT_OK)
