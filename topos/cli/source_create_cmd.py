"""Agent-friendly private source creation over the local owner socket.

Inference/checking are read-only. Installation needs a person's terminal approval,
optionally opened by the agent. A file digest binds that handoff to what was checked.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import click

from topos.sources.local_create import MAX_BYTES, digest, infer_sample

PREPARE = "/api/local/owner/sources/prepare"
INSTALL = "/api/local/owner/sources/install-private"


def _socket():
    from topos.mcp_server import NodeSocket
    return NodeSocket(timeout_s=600.0)


def _read(path: Path) -> str:
    with path.open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise click.ClickException("File exceeds 2 MiB; use a representative sample")
    return content.decode("utf-8-sig")


def _call(sock, method, path, **kwargs):
    from topos.mcp_server import NodeDown, NodeRefused
    try:
        return sock.request_sync(method, path, **kwargs)
    except NodeDown:
        click.echo('Topos isn\'t running — say "start Topos".', err=True)
        raise click.exceptions.Exit(4)
    except NodeRefused as exc:
        if exc.status == 404:
            raise click.ClickException("This node does not support private source creation yet. Update Topos first.")
        # Never print an arbitrary server body: it may contain sample text or credentials.
        raise click.ClickException(f"Topos refused the request ({exc.code or exc.status}).")


def _emit(doc: dict, as_json: bool, message: str) -> None:
    click.echo(json.dumps(doc, indent=2, ensure_ascii=True) if as_json else message)


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _plain(value) -> str:
    """One printable line: control characters (newlines, ESC) are shown escaped, never acted on by the terminal."""
    text = str(value)
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in text)


_PREVIEW_CHARS = 400


def _summary(plan: dict) -> str:
    definition = plan["source_definition_json"]
    sample = plan.get("sample")
    lane = definition.get("canonical_group_id")
    text = (f"Add {_plain(definition['display_name'])} ({plan['source_id']}) to the Topos on this computer.\n"
            f"Definition stays unregistered. Delivery: {definition['delivery']}; "
            f"mapping: {definition.get('canonical_group_id') or 'raw-only'}; "
            f"posture: {definition['posture']}; enrichment: {definition['enrichment_trigger']}.\n")
    if plan.get("target"):
        text += f"Target Topos: {plan['target']['topos_id']}.\n"
    if sample:
        text += (f"Import {_plain(sample['file_name'])} ({sample['bytes']} bytes): "
                 f"{sample['records_checked']} records, every one checked.\n")
        if lane in {"conversations", "ai_messages"}:
            text += (f"These records are stored as {lane} written by you: Topos reads them as your own words, "
                     f"extracts facts from them, and shares that lane as your existing grants allow.\n")
        preview = sample.get("normalized_preview") or []
        if preview:
            text += f"First {len(preview)} of them as they will be stored:\n"
            for row in preview:
                line = json.dumps(row, ensure_ascii=True, sort_keys=True)
                text += "  " + (line if len(line) <= _PREVIEW_CHARS else line[:_PREVIEW_CHARS] + " ...") + "\n"
    else:
        text += "Install the definition only; no records will be imported.\n"
    text += "\n".join(plan.get("warnings") or [])
    return text


@click.command("infer")
@click.argument("sample", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--record-path", default="", help="Nested record array, e.g. items[*].")
@click.option("--json", "as_json", is_flag=True)
def infer_command(sample: Path, record_path: str, as_json: bool) -> None:
    """Inspect JSON/JSONL/CSV locally; no account, key, node or upload needed."""
    try:
        result = infer_sample(sample.name, _read(sample), record_path=record_path)
    except (ValueError, TypeError, OSError) as exc:
        raise click.ClickException(str(exc)) from None
    _emit(result, as_json, json.dumps(result, indent=2, ensure_ascii=True))


@click.command("add")
@click.argument("definition", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--sample", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Check and, after approval, import this file (up to 2 MiB).")
@click.option("--check", is_flag=True, help="Validate on the running node without installing or importing.")
@click.option("--open-terminal", is_flag=True, help="Open the owner's approval prompt on macOS.")
@click.option("--expected-input-sha256", hidden=True, default="")
@click.option("--json", "as_json", is_flag=True)
def add_command(definition: Path, sample: Path | None, check: bool, open_terminal: bool,
                expected_input_sha256: str, as_json: bool) -> None:
    """Install private source JSON, optionally import a sample, after one owner approval.

    --check is agent-safe. There is no --yes flag and no registry/publication option.
    """
    try:
        body = {"source_definition_json": json.loads(_read(definition))}
        if sample is not None:
            body["sample"] = {"file_name": sample.name, "content": _read(sample)}
        fingerprint = digest(body)
    except (ValueError, TypeError, OSError) as exc:
        raise click.ClickException(str(exc)) from None
    if expected_input_sha256 and fingerprint != expected_input_sha256:
        raise click.ClickException("The definition or sample changed after preparation. Check it again.")
    sock = _socket()
    plan = _call(sock, "POST", PREPARE, json_body=body)
    if plan.get("status") != "ready":
        _emit(plan, as_json, "Source is not ready:\n" + "\n".join(plan.get("errors") or ["Invalid plan"]))
        raise click.exceptions.Exit(1)
    if check:
        _emit(plan, as_json, _summary(plan) + "\nChecked only; nothing installed or imported.")
        return
    if sample is not None and plan["source_definition_json"]["source_type"] != "file":
        raise click.ClickException("Use --sample --check to preview streams; install without --sample and enroll a producer.")
    if not _tty():
        argv = ["sources", "add", str(definition.resolve()), "--expected-input-sha256", fingerprint]
        if sample is not None:
            argv += ["--sample", str(sample.resolve())]
        command = shlex.join(["topos-node", *argv])
        opened = False
        if open_terminal:
            from topos.cli.terminal_window import open_terminal_window
            opened = open_terminal_window(slug="private-source", title="Add a private source to your Topos",
                argv=argv, done="The source command finished. Check its receipt above.",
                not_done="The source was not fully added. Read the result above before retrying.")
        doc = {"status": "approval_pending" if opened else "needs_human", "installed": False,
               "source_id": plan["source_id"], "plan_sha256": plan["plan_sha256"], "command": command}
        _emit(doc, as_json, "Approve the source in the Terminal window." if opened else
              f"Run this in your own terminal and review the source:\n{command}")
        raise click.exceptions.Exit(2)
    # Use the in-memory payload shown here. Neither the definition nor sample is reread after approval.
    click.echo(_summary(plan), err=True)
    click.echo("Definition:\n" + json.dumps(plan["source_definition_json"], indent=2, ensure_ascii=True), err=True)
    if not click.confirm("Add this private source" + (" and import this sample" if sample else "") + "?",
                         default=False, err=True):
        _emit({"status": "cancelled", "installed": False}, as_json, "Nothing changed.")
        return
    from topos.uds import write_owner_confirmation
    result = _call(sock, "POST", INSTALL, json_body={**body, "plan_sha256": plan["plan_sha256"]},
                   headers={"X-Topos-Owner-Confirm": write_owner_confirmation()})
    _emit(result, as_json, json.dumps(result, indent=2, ensure_ascii=True))
    if result.get("status") not in {"installed", "ingested"}:
        raise click.exceptions.Exit(1)


@click.command("inspect")
@click.argument("source_id")
@click.option("--json", "as_json", is_flag=True)
def inspect_command(source_id: str, as_json: bool) -> None:
    """Read installed definitions for a source on this Topos; never imports or publishes."""
    from topos.sources.local_create import ID_RE
    if not ID_RE.fullmatch(source_id):
        raise click.ClickException("Invalid source id")
    sock = _socket()
    doc = _call(sock, "GET", f"/api/local/owner/private-sources/{source_id}")
    _emit(doc, as_json, json.dumps(doc, indent=2, ensure_ascii=True))
