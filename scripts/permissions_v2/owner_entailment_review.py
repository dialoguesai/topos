"""OD-38 owner confirmation, from the owner's own terminal.

The owner-confirmed entailment route (`POST /v1/permissions-beta/v2/message-search/entailment-review`) has no
web page: the control plane relays an owner handler only through its allow-list and a per-feature proxy. This
is the owner's way to use it. It talks to the node over the owner socket (`~/.topos/engine.sock`, mode 0600,
which only the owner's own processes can open), lists the claims the guards left, shows each one beside the
message it cites, and asks: confirm, reject, skip or quit. Each answer is the route's own confirm or reject,
so every rule stays the node's: rejects are sticky, a claim that changed since the list is refused as stale,
and every decision rebuilds the grant indexes.

What it will not do:
  * run without a terminal. Stdin and stdout must both be a TTY, so a pipe, a file, a log or an agent's shell
    never receives a claim or a message;
  * write anything: no file, no log, no cache. The claims exist only on the owner's screen;
  * decide anything itself. There is no "confirm all".

Run it yourself (the node's own Python has everything it needs):
  ~/.local/share/uv/tools/topos-node/bin/python scripts/permissions_v2/owner_entailment_review.py

The node must have `TOPOS_PERMISSIONS_V2_ENTAILMENT_GROUNDING=true` (and, for OD-45,
`TOPOS_PERMISSIONS_V2_ENTAILMENT_SENTENCE_REPORTING=true`) in ~/.topos/.env, then a restart.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Callable

ROUTE = "/v1/permissions-beta/v2/message-search/entailment-review"
SOCKET = "~/.topos/engine.sock"
CONFIG = "~/.topos/permissions-v2/config.json"
BINDING_FIELDS = ("environment_id", "node_id", "resource_id", "owner_id")


class Refused(RuntimeError):
    """A reason to stop, said in words the owner can act on. Never carries a claim."""


def binding(config_path: Path) -> dict:
    """The node's evidence binding, from the node's own permissions config (what the route checks against)."""
    try:
        identity = json.loads(Path(config_path).expanduser().read_text())["identity"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Refused("cannot read the node's permissions config at " + str(config_path)) from None
    try:
        from topos.permissions_v2.evidence import EvidenceBinding
        fields = tuple(EvidenceBinding.model_fields)
    except Exception:  # noqa: BLE001 -- outside the node's Python: fall back to the fields as shipped
        fields = BINDING_FIELDS
    missing = [key for key in fields if key not in identity]
    if missing:
        raise Refused("the node's permissions config has no binding field: " + ", ".join(missing))
    return {key: identity[key] for key in fields}


def post(client, body: dict):
    response = client.post(ROUTE, json=body)
    try:
        data = response.json()
    except ValueError:
        data = {}
    return response.status_code, data


def render(index: int, total: int, candidate: dict, width: int = 100) -> str:
    wrap = lambda text: textwrap.fill(str(text), width=width, initial_indent="    ", subsequent_indent="    ")  # noqa: E731
    return "\n".join([f"--- {index} of {total} ({candidate.get('kind', '?')}) ---",
                      "  claim:", wrap(candidate.get("claim", "")),
                      "  the message it cites:", wrap(candidate.get("message", "")), ""])


ANSWERS = {"c": "confirm", "confirm": "confirm", "r": "reject", "reject": "reject",
           "s": "skip", "skip": "skip", "": "skip", "q": "quit", "quit": "quit"}


def review(client, bind: dict, *, ask: Callable[[str], str], show: Callable[[str], None]) -> dict:
    """List, then one decision per pending claim. Returns what happened, as words for the owner's screen."""
    status, data = post(client, {"binding": bind, "operation": "list"})
    if status == 404:
        raise Refused("OD-38 owner review is off on this node: set TOPOS_PERMISSIONS_V2_ENTAILMENT_GROUNDING=true "
                      "in ~/.topos/.env and restart the node")
    if status == 403:
        raise Refused("the node refused: this is not the owner's socket, or the binding is not this node's")
    if status != 200 or not isinstance(data.get("candidates"), list):
        raise Refused(f"the node could not list claims (HTTP {status})")
    pending = [c for c in data["candidates"] if isinstance(c, dict) and c.get("status") == "pending"
               and isinstance(c.get("candidate_id"), str)]
    outcome = {"pending": len(pending), "confirmed": 0, "rejected": 0, "skipped": 0, "stale": 0}
    if not pending:
        show("Nothing to decide: no claim is waiting for you.")
        return outcome
    for index, candidate in enumerate(pending, 1):
        show(render(index, len(pending), candidate))
        answer = None
        while answer is None:
            answer = ANSWERS.get(ask("[c]onfirm  [r]eject  [s]kip  [q]uit > ").strip().lower())
        if answer == "quit":
            outcome["skipped"] += len(pending) - index + 1
            break
        if answer == "skip":
            outcome["skipped"] += 1
            continue
        status, _ = post(client, {"binding": bind, "operation": answer, "candidate_id": candidate["candidate_id"]})
        if status == 200:
            outcome["confirmed" if answer == "confirm" else "rejected"] += 1
            show("  " + ("confirmed" if answer == "confirm" else "rejected") + ".")
        elif status == 409:
            outcome["stale"] += 1
            show("  not recorded: the claim or its message changed since the list, or you rejected it before.")
        else:
            raise Refused(f"the node refused that decision (HTTP {status}); nothing after it was sent")
    return outcome


def open_client(socket_path: Path):
    import httpx
    path = Path(socket_path).expanduser()
    if not path.exists():
        raise Refused(f"no owner socket at {path}: is the node running?")
    return httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), base_url="http://topos-owner", timeout=600)


def main(argv=None, *, stdin=None, stdout=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", type=Path, default=Path(os.environ.get("TOPOS_UDS_PATH") or SOCKET))
    parser.add_argument("--config", type=Path, default=Path(CONFIG))
    args = parser.parse_args(argv)
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if not (stdin.isatty() and stdout.isatty()):
        print("owner_entailment_review: refusing to run without a terminal. It shows your own messages, so it "
              "only runs where you are the one reading: run it yourself, in a terminal.", file=sys.stderr)
        return 2
    try:
        bind = binding(args.config)
        with open_client(args.socket) as client:
            outcome = review(client, bind, ask=input, show=lambda text: print(text, file=stdout, flush=True))
    except Refused as exc:
        print("owner_entailment_review: " + str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- a transport error names its kind, never a claim
        hint = (" (if TOPOS_UDS_TEAM_IDS is set on the node, the socket admits only signed apps)"
                if type(exc).__name__ in {"ConnectError", "RemoteProtocolError", "ReadError"} else "")
        print(f"owner_entailment_review: could not reach the node: {type(exc).__name__}{hint}", file=sys.stderr)
        return 1
    print(f"Done. Confirmed {outcome['confirmed']}, rejected {outcome['rejected']}, skipped {outcome['skipped']}, "
          f"changed since listed {outcome['stale']}.", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
