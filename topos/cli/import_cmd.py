"""`topos-node import` — put a file that is already on this computer into Topos.

No account, no network: the file's path goes to the running node over the owner
socket, and the node reads it where it is. The bundled file sources today are
the ChatGPT export (``chatgpt_file_ingestion``, the default), YouTube
transcripts and the demo CSVs. ``--list`` shows the exports the node can find
on its own (Downloads and the like).

Writing rows is a dangerous change on the owner socket (``owner_door_routes``):
it needs the human, asked here on a terminal, and a one-change confirmation
token — never a flag an agent could pass. An agent that wants a file imported
tells the person to run this command in their own terminal.

What never happens here: the node's answer is read for its status word and its
counts; no key is read or printed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import click

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NEEDS_HUMAN = 2
EXIT_NODE_DOWN = 4

CHATGPT_SOURCE = "chatgpt_file_ingestion"
#: The ingestion routes are mounted under ``/v1`` (``app.py``). In 1.5.45 the CLI posted them without it, so the
#: default import and ``--list`` both got 404 (7 Nov 2026); tests/cli/test_import_cmd.py now checks every path the
#: command sends against the app's own routes.
LOCAL_EXPORTS_PATH = "/v1/ingestion/local-exports"
UPLOAD_LOCAL_PATH = "/v1/ingestion/upload-local-path"

_JSON_OPTION = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")


def _socket():
    from topos.mcp_server import NodeSocket

    return NodeSocket(timeout_s=600.0)


def _file_sources() -> Dict[str, str]:
    # Runtime installs live in the running node, not this fresh CLI process's registry.
    from topos.mcp_server import NodeDown, NodeRefused
    try:
        result = _socket().request_sync("GET", "/sources")
    except NodeDown:
        raise click.ClickException('Topos isn\'t running — say "start Topos".') from None
    except NodeRefused as exc:
        raise click.ClickException(f"Topos refused the source list ({exc.code or exc.status}).") from None
    return {d["source_id"]: d.get("display_name") or d["source_id"] for d in result.get("sources", [])
            if d.get("delivery") == "owner_upload" or (not d.get("delivery") and d.get("source_type") == "file")}


def _emit(doc: Dict[str, Any], *, as_json: bool, lines: list[str]) -> None:
    if as_json:
        click.echo(json.dumps(doc, sort_keys=True))
    else:
        for line in lines:
            click.echo(line)


def _fail(message: str, *, as_json: bool, code: int = EXIT_FAILED) -> None:
    if as_json:
        click.echo(json.dumps({"status": "error", "error": message, "exit_code": code}))
    else:
        click.echo(message, err=True)
    sys.exit(code)


def _scope(sock, *, as_json: bool) -> Dict[str, Any]:
    from topos.mcp_server import NodeDown, NodeRefused

    try:
        return sock.request_sync("GET", "/api/local/owner/scope")
    except NodeDown:
        _fail("Topos isn't running — say \"start Topos\".", as_json=as_json, code=EXIT_NODE_DOWN)
    except NodeRefused as exc:
        _fail(f"Topos has no identity yet ({exc.code or exc.status}); start it once, then import.", as_json=as_json)
    return {}  # unreachable


@click.command("import")
@click.argument("path", required=False, type=click.Path(path_type=Path))
@click.option("--source", "source_id", default=CHATGPT_SOURCE, show_default=True,
              help="Which file source this is (see --sources).")
@click.option("--list", "list_exports", is_flag=True, help="Show exports the node can find on this computer.")
@click.option("--sources", "show_sources", is_flag=True, help="Show the file sources that accept an import.")
@_JSON_OPTION
def import_command(path: Optional[Path], source_id: str, list_exports: bool, show_sources: bool, as_json: bool) -> None:
    """Import a file from this computer (default: a ChatGPT export) into Topos."""
    from topos.mcp_server import NodeDown, NodeRefused

    sources = _file_sources()
    if show_sources:
        _emit({"sources": sources}, as_json=as_json,
              lines=[f"{sid}  {name}" for sid, name in sorted(sources.items())])
        return
    sock = _socket()
    if list_exports:
        try:
            found = sock.request_sync("GET", LOCAL_EXPORTS_PATH)
        except NodeDown:
            _fail("Topos isn't running — say \"start Topos\".", as_json=as_json, code=EXIT_NODE_DOWN)
        except NodeRefused as exc:
            _fail(f"Topos refused the scan ({exc.code or exc.status}).", as_json=as_json)
        items = found.get("exports") or found.get("items") or []
        _emit(found, as_json=as_json,
              lines=[f"{it.get('label') or it.get('name') or it.get('handle')}" for it in items if isinstance(it, dict)]
              or ["No exports found in the usual folders. Pass a path instead."])
        return
    if path is None:
        _fail("Give the file to import, or --list to see exports found on this computer.", as_json=as_json)
    if source_id not in sources:
        _fail(f"{source_id} does not take a file import. File sources: {', '.join(sorted(sources))}.", as_json=as_json)
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        _fail(f"No file at {resolved}.", as_json=as_json)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _fail("Importing a file writes into your Topos, so it asks you first: run this command in your own terminal.",
              as_json=as_json, code=EXIT_NEEDS_HUMAN)

    scope = _scope(sock, as_json=as_json)
    dataset_id = str(scope.get("dataset_id") or "")
    size_kb = max(1, resolved.stat().st_size // 1024)
    if not click.confirm(f"Import {resolved.name} ({size_kb} KB) into {sources[source_id]}?", default=False, err=True):
        _fail("Nothing imported.", as_json=as_json, code=EXIT_OK)
    from topos.uds import write_owner_confirmation

    confirm = {"X-Topos-Owner-Confirm": write_owner_confirmation()}
    try:
        if source_id == CHATGPT_SOURCE:
            result = sock.request_sync("POST", UPLOAD_LOCAL_PATH,
                                       json_body={"dataset_id": dataset_id, "file_path": str(resolved)},
                                       headers=confirm)
        else:
            result = sock.request_sync("POST", f"/sources/{source_id}/ingest",
                                       params={"dataset_id": dataset_id, "file_path": str(resolved)},
                                       headers=confirm)
    except NodeDown:
        _fail("Topos stopped while importing. Check `topos-node status`.", as_json=as_json, code=EXIT_NODE_DOWN)
    except NodeRefused as exc:
        _fail(f"Topos refused the import ({exc.code or exc.status}).", as_json=as_json)
    status = str(result.get("status") or "")
    if status == "error":
        _fail(f"Import failed: {result.get('error')}", as_json=as_json)
    doc = {"status": status or "accepted", "source_id": source_id, "file": resolved.name,
           "mode": scope.get("identity_mode"), "result": {k: v for k, v in result.items() if k != "status"}}
    _emit(doc, as_json=as_json, lines=[
        f"Imported {resolved.name} into {sources[source_id]} ({status or 'accepted'}).",
        "Processing continues in the background; `topos-node status` shows the source.",
    ])
