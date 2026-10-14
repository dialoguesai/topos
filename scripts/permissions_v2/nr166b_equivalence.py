#!/usr/bin/env python3
"""NR166B: the Off-limits boundary's pass memory changes no answer, at 100, 300 and 600 members.

On the load harness's fixture (`request_load_harness.build_fixture`: invented messages under one knowledge share,
an active Off-limits boundary, an identity universe no member touches), everything below is done twice, with
`entity_boundary.PASS_MEMORY` off (the code before NR166B) and on, and the two are compared:

- decisions: for every member, `observe` on its row as the member loop reads it (the loaded row with its provenance
  columns, then the live row twice), and on the same live row with a protected name written into it in three
  spellings; its review context (`context_for`); and `mentions_protected` over its neighbours' texts, one at a time
  and together. All inside one `one_pass` over one read, as the loop runs them. Outcomes, refusals included;
- both doors: every question and every batch, the released bytes (`Doors.ask`'s hash) and, for a batch, each
  item's receipt and decision as the ledger wrote them (request ids and times masked: they differ by request);
- the request's check and the sweep's deep check (`_current`), on the share as built and after each change of
  `tests/permissions_v2/test_nr166b_member_loop.py` (each restored after), verdict and stale class;
- a build: the sealed members (unsealed and compared whole) and the basis.

Writes `<out>/equivalence.json`; exits 1 on any difference. Run from the engine root with scratch TOPOS_DATABASE_PATH
and TOPOS_ENV_FILE, as the harness:
    <topos venv python> scripts/permissions_v2/nr166b_equivalence.py --members 100,300,600 --out <dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(Path(__file__).resolve().parent))

import request_load_harness as harness  # noqa: E402  (sets TOPOS_KEY for the fixture, as the harness does)

SPELLINGS = (" Mara Example", " M.E.", " Mara​ Example")   # the twins' protected person (direct_search_twins)


def outcome(call):
    from topos.permissions_v2.canonical import PolicyError
    try:
        return ["answer", call()]
    except PolicyError as exc:
        return ["refused", exc.code]
    except Exception as exc:  # noqa: BLE001 -- the type is what is compared
        return ["raised", type(exc).__name__]


def members_of(fixture):
    from topos.permissions_v2.search_index import index_path, unseal
    index = fixture.node.index
    opened = index._open(index_path(index.root, fixture.grant_id))
    key = index.keys.get(fixture.grant_id, create=False)
    return [unseal(key, opaque, sealed) for opaque, sealed in opened["sealed"]]


def decisions(fixture, memory: bool) -> list:
    from topos.permissions_v2 import entity_boundary
    from topos.permissions_v2.automatic_message_review import context_for
    from topos.permissions_v2.entity_boundary import EntityBoundary
    from topos.permissions_v2.evidence import EvidenceIdentity
    from topos.permissions_v2.search_index import _live_rows
    entity_boundary.PASS_MEMORY = memory
    resolver = fixture.node.index.resolver
    members = members_of(fixture)
    found = []
    with resolver._read(gated=False) as (conn, _floor):
        boundary = EntityBoundary(conn)
        with boundary.one_pass():
            assert (boundary._pass is not None) is memory
            for member in members:
                if "message" not in member:
                    continue
                identity = EvidenceIdentity.parse(member["message"])
                fields = {"table": identity.table, "record_id": identity.record_id, "source_id": identity.source_id,
                          "dataset_id": identity.dataset_id}
                loaded = outcome(lambda: resolver._load(conn, identity))
                rows, _facts = _live_rows(conn, member)
                live = dict(rows[0])
                for row in ([loaded[1]] if loaded[0] == "answer" else []) + [live, live]:
                    found.append(outcome(lambda: list(boundary.observe(**fields, row=row))))
                for spelling in SPELLINGS:
                    found.append(outcome(lambda: list(boundary.observe(
                        **fields, row={**live, "content": (live.get("content") or "") + spelling}))))
                found.append(outcome(lambda: context_for(conn, identity, live, boundary=boundary)[0]))
                texts = [text for text, in conn.execute(
                    "SELECT content FROM conversation_messages WHERE conversation_id=? AND message_id<>? "
                    "ORDER BY event_at, message_id LIMIT 4", (live.get("conversation_id"), identity.record_id))]
                found.append(outcome(lambda: [boundary.mentions_protected(text) for text in texts]))
                found.append(outcome(lambda: boundary.mentions_protected(*texts)))
                found.append(outcome(lambda: boundary.mentions_protected(*texts, "and Mara Example")))
    return found


def _masked(value, ids):
    """A receipt with what differs by request masked: its ids, its times, and the hash of its signed envelope (which
    carries both)."""
    if isinstance(value, dict):
        return {key: ("<envelope>" if key == "envelope_hash" else
                      "<time>" if (key.endswith("_at") or key in {"now", "time", "issued", "expires"})
                      and isinstance(item, (int, float)) else _masked(item, ids)) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item, ids) for item in value]
    if isinstance(value, str):
        for real, mask in ids:
            value = value.replace(real, mask)
        return value
    return value


def receipts(fixture, request_id: str) -> list:
    with sqlite3.connect(fixture.node.ledger.path) as db:
        rows = db.execute("SELECT request_id, receipt_json, decision_json FROM p2a_receipts WHERE request_id LIKE ? "
                          "ORDER BY request_id", (request_id + ":%",)).fetchall()
    return [[_masked(json.loads(receipt), [(request_id, "<id>")]), _masked(json.loads(decision), [(request_id, "<id>")])]
            for _id, receipt, decision in rows]


def doors_compared(fixture, doors, memory: bool, tag: str) -> list:
    from topos.permissions_v2 import entity_boundary
    entity_boundary.PASS_MEMORY = memory
    found = []
    for door in ("answers", "batch"):
        for number, unit in enumerate(doors.units[door]):
            request_id = f"nr166b-{tag}-{door}-{number}"
            prepared = doors.prepare(door, unit, request_id)
            released = outcome(lambda: doors.ask(door, prepared))
            found.append([door, number, released, receipts(fixture, request_id) if door == "batch" else []])
    return found


def verdicts(fixture, memory: bool) -> dict:
    import pytest
    from tests.permissions_v2 import test_nr166b_member_loop as loop
    from topos.permissions_v2 import entity_boundary
    entity_boundary.PASS_MEMORY = memory
    node = fixture.node
    saved = sqlite3.connect(":memory:")
    with sqlite3.connect(node.corpus.path) as source:
        source.backup(saved)
    patch = pytest.MonkeyPatch()
    found = {}
    try:
        for change, statements in sorted(loop.CHANGES.items()):
            message, conversation = loop.a_member(node)
            with sqlite3.connect(node.corpus.path) as conn:
                for sql in statements:
                    conn.execute(sql, {"message": message, "conversation": conversation})
            found[change] = [list(loop.verdict(node, deep=deep, monkeypatch=patch)) for deep in (False, True)]
            with sqlite3.connect(node.corpus.path) as target:
                saved.backup(target)
    finally:
        patch.undo()
    return json.loads(json.dumps(found, default=str))


def build(fixture, memory: bool):
    from tests.permissions_v2.message_search_harness import owner
    from topos.permissions_v2 import entity_boundary
    from topos.permissions_v2.search_index import index_path
    entity_boundary.PASS_MEMORY = memory
    index = fixture.node.index
    with owner():
        state = index.rebuild(fixture.grant_id, now=fixture.node.now[0])
    basis = index._open(index_path(index.root, fixture.grant_id))["basis"]
    members = sorted(json.dumps(member, sort_keys=True) for member in members_of(fixture))
    return {"state": state, "basis": basis, "members": members}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--members", default="100,300,600")
    parser.add_argument("--units", type=int, default=6, help="distinct questions, and distinct batches")
    parser.add_argument("--universe", type=int, default=10)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--scratch", type=Path)
    args = parser.parse_args(argv)
    import topos
    from topos.permissions_v2 import entity_boundary
    scratch = args.scratch or Path(tempfile.mkdtemp(prefix="nr166b-equivalence-"))
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"schema": "NR166B-equivalence/v1", "engine": str(Path(topos.__file__).parent), "sizes": []}
    bad = 0
    for members in [int(value) for value in args.members.split(",")]:
        started = time.monotonic()
        fixture = harness.build_fixture(scratch / f"members-{members}-{int(time.time())}", members=members, spare=10,
                                        seed=args.seed, universe=args.universe)
        doors = harness.Doors(fixture, batch_size=3, one_share=False, units=args.units)
        doors.ask("answers", doors.prepare("answers", doors.units["answers"][0], "nr166b-warm-up"))
        size = {"members": members}
        for name, run in (("decisions", lambda memory: decisions(fixture, memory)),
                          ("doors", lambda memory: doors_compared(fixture, doors, memory, "on" if memory else "off")),
                          ("verdicts", lambda memory: verdicts(fixture, memory)),
                          ("build", lambda memory: build(fixture, memory))):
            off, on = run(False), run(True)
            same = off == on
            bad += not same
            size[name] = {"same": same, "compared": len(off) if isinstance(off, (list, dict)) else 1,
                          "digest_off": digest(off), "digest_on": digest(on)}
            if name == "decisions":
                size[name]["protected_answers"] = sum(1 for item in on if item[0] == "answer" and isinstance(item[1], list)
                                                      and item[1] and item[1][0] is True)
                size[name]["refusals"] = sum(1 for item in on if item[0] != "answer")
            if name == "doors":
                size[name]["refused"] = sum(1 for item in on if item[2][0] != "answer")
                size[name]["receipts"] = sum(len(item[3]) for item in on)
            if name == "verdicts":
                size[name]["classes"] = {change: [[current, [fields.get("reason") for _kind, fields in stages]]
                                                  for current, stages in pair] for change, pair in on.items()}
            if name == "build":
                size[name]["sealed"] = len(on["members"])
            if not same:
                (args.out / f"difference-{members}-{name}.json").write_text(json.dumps({"off": off, "on": on},
                                                                                       default=str, indent=1))
            print(f"members={members} {name}: same={same} compared={size[name]['compared']}", flush=True)
        entity_boundary.PASS_MEMORY = True
        size["seconds"] = round(time.monotonic() - started, 1)
        report["sizes"].append(size)
    report["differences"] = bad
    (args.out / "equivalence.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({"differences": bad}))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
