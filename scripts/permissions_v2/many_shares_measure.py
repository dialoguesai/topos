"""N3: ten shares on one in-process node, invented data. What one owner change costs, before and after N3.

For one signed change of one share among ten (a new policy version that lowers its k, so its index must be
rebuilt), per round:

  ack_seconds          from the relay handing the change to the node until the signed acknowledgement is back
                       (its life is 120 s)
  indexes_rebuilt      index files the change rebuilt (file identity before and after, every share)
  builds               index builds the change ran
  writer_wait_seconds  a writer that asks for the node write gate while the rebuild runs (any other write on the
                       node, ingestion's included): how long it waited
  index_work_seconds   from the change applied until its index work ended
  longest_gate_hold    the longest single hold of the write gate in the round (any thread), and
  rebuild_gate_holds   the rebuild queue's own holds (after N3 only): how many, and the longest

Then one change of only a share's daily number (`light`): N3 re-stamps that index's basis instead of building
(A2A-4 Q5). Then one owner review change (`review`): the index part of the review hook, timed the same way; every share is
rebuilt, and the p2c-v1 indexes the guard cannot see a review change in are dropped inside the change's own gate hold
(`change_gate_hold` is that hold).

`before` replays the owner hook as it was until N3 (sweep, then rebuild every share, under the write gate, before
the acknowledgement is returned) on its own fresh node; `after` sends the change through the real handler and waits
for the runtime's rebuild queue. `--embed-ms N` adds N ms per member to every build's ungated phase, standing in for
the passage embedding a real node does (RD3 measured 8-18 ms per member on a loaded host); 0 measures the synthetic
corpus as it is.

Synthetic only: everything is written under a temporary directory in TMPDIR; no server, no port, no real database,
no model. Run from the engine root:
    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/many_shares_measure.py --out report.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TOPOS_KEY", "synthetic-timing-key")
os.environ["TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED"] = "true"

SHARES = [f"share-{number:02d}" for number in range(10)]


def share_policy(mc, number: int, version: str | None = None, *, max_k: int = 25, budget: int | None = None) -> dict:
    raw = mc.search_policy(grant=SHARES[number], actor=f"person-{number:02d}", client="client-2", max_k=max_k)
    if version:
        raw["policy_version_id"] = version
    if budget is not None:
        raw["read_budget_per_day"] = budget
    return raw


def build_node(root: Path, *, members: int, seed: int):
    from tests.permissions_v2 import message_search_corpus as mc
    from tests.permissions_v2.message_search_harness import Node, embed_corpus
    mc.NOW = int(time.time())       # the node runs on the wall clock, as the owner hooks read it
    corpus = mc.build(root / "corpus", seed=seed, counts={name: 1 for name in mc.KINDS} | {"clean_positive_C": members})
    embed_corpus(corpus)
    node = Node(corpus, root / "node", search_raw=share_policy(mc, 0), now=mc.NOW)
    for number in range(1, len(SHARES)):
        node.activate(share_policy(mc, number))
    states = node.rebuild()
    if any(states[grant] != "ready" for grant in SHARES):
        raise SystemExit("the ten shares did not build")
    node.mc = mc
    return node


def identities(node) -> dict:
    from topos.permissions_v2.search_index import index_path
    found = {}
    for grant in SHARES:
        try:
            info = os.stat(index_path(node.index.root, grant))
            found[grant] = (info.st_ino, info.st_mtime_ns)
        except FileNotFoundError:
            found[grant] = None
    return found


class Hooks:
    """Counts builds, marks the first ungated build phase of a round, and adds the per-member work of --embed-ms."""

    def __init__(self, embed_ms: float):
        from topos.permissions_v2.search_index import SearchIndexService
        self.embed_ms, self.builds, self.building = embed_ms, 0, threading.Event()
        real_rebuild, real_members = SearchIndexService._rebuild, SearchIndexService._members
        hooks = self

        def rebuild(service, grant_id, *, now=None):
            hooks.builds += 1
            return real_rebuild(service, grant_id, now=now)

        def members(service, conn, key, grant_id, entries, model):
            hooks.building.set()
            if hooks.embed_ms:
                time.sleep(hooks.embed_ms * len(entries) / 1000)
            return real_members(service, conn, key, grant_id, entries, model)
        SearchIndexService._rebuild, SearchIndexService._members = rebuild, members

    def reset(self):
        self.builds = 0
        self.building = threading.Event()


class GateHolds:
    """Every outermost hold of the node write gate: (thread name, seconds held)."""

    def __init__(self):
        from topos.storage.db import write_gate
        self.holds = []
        real_clear = write_gate._clear_holder
        recorder = self

        def clear():
            holder = write_gate._holder
            if holder is not None and holder.ident == threading.get_ident() and write_gate._holder_depth == 1:
                recorder.holds.append((holder.thread, time.monotonic() - holder.since))
            real_clear()
        write_gate._clear_holder = clear

    def take(self) -> list:
        taken, self.holds = self.holds, []
        return taken


def writer(hooks: Hooks, waits: list, *, patience: float = 120) -> threading.Thread:
    from topos.storage.db.write_gate import with_db_write
    building = hooks.building

    def run():
        if building.wait(patience):
            began = time.perf_counter()
            with with_db_write():
                waits.append(time.perf_counter() - began)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def signed_change(node, number: int, *, generation: int, round_number, light: bool = False):
    """A new policy version for share `number`: a lower k (its permitted set's build must run again), or with `light`
    the same policy with a daily number (no build reads it)."""
    from topos.permissions_v2.canonical import digest
    from topos.permissions_v2.protocol import MutationBody, sign_mutation
    from topos.permissions_v2.registry import parse_policy
    version = f"policy-{SHARES[number]}-r{round_number}"
    raw = (share_policy(node.mc, number, version=version, budget=50) if light
           else share_policy(node.mc, number, version=version, max_k=20))
    policy = parse_policy(raw).model_dump()
    with node.ledger._transaction() as conn:
        node.protocol._sync_protection(conn)
        state = node.ledger._node(conn)
    now = int(time.time())
    authority = {**policy["binding"], "grant_generation": generation, "assignment_generation": generation,
                 "policy_version_id": policy["policy_version_id"], "policy_hash": digest(policy),
                 "capability_version": policy["versions"]["capability"],
                 "protection_revision": state["protection_revision"], "node_epoch": state["epoch"] + 1}
    return sign_mutation(MutationBody.parse({
        "version": "topos-policy-mutation/v2", "kid": "cp-key", "issuer_id": "cp-issuer",
        "audience_id": node.ledger.identity.node_id, "command_id": f"change-{round_number}", "operation": "activate",
        "expected_epoch": state["epoch"], "authority": authority, "policy": policy,
        "owner_authorization": {"actor_id": node.mc.OWNER_ID, "client_id": "owner-ui"},
        "issued_at": now, "expires_at": now + 120}), node.cp_key)


def before_round(node, change, hooks: Hooks) -> dict:
    """The owner hook as it was until N3: apply, sign, then sweep and rebuild every share, all under the gate."""
    from topos.permissions_v2.search_index import forget_inactive_record_keys
    from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
    from topos.storage.db.write_gate import with_db_write
    waits, marks = [], {}

    def apply():
        token = set_principal(Principal(cls=OWNER_APP, channel="cp_relay", acting_user=node.mc.OWNER_ID))
        try:
            with with_db_write():
                ack = node.protocol.mutate(change.model_dump(), now=int(time.time()))
                marks["applied"] = time.perf_counter()
                if ack.outcome == "applied":
                    forget_inactive_record_keys(node.ledger, node.index.root, now=int(time.time()))
                    node.index.sweep()
                    node.index.rebuild_all()
                marks["work_done"] = time.perf_counter()
                return ack
        finally:
            reset_principal(token)
    thread = writer(hooks, waits)
    started = time.perf_counter()
    ack = asyncio.run(asyncio.to_thread(apply))
    returned = time.perf_counter()
    thread.join(120)
    return {"outcome": ack.outcome, "ack_seconds": returned - started, "writer_wait_seconds": waits[0] if waits else None,
            "index_work_seconds": marks["work_done"] - marks["applied"]}


def after_round(node, change, hooks: Hooks, rebuilds, *, patience: float = 120) -> dict:
    """N3: the real handler; the index work runs on the rebuild queue after the acknowledgement."""
    from topos.core.handlers import handle_control_plane_request
    from topos.principal import OWNER_APP, Principal
    waits = []
    thread = writer(hooks, waits, patience=patience)
    started = time.perf_counter()
    reply = asyncio.run(handle_control_plane_request(
        {"id": change.command_id, "type": "permissions_v2_mutate", "payload": {"envelope": change.model_dump()}},
        principal=Principal(cls=OWNER_APP, channel="cp_relay", acting_user=node.mc.OWNER_ID)))
    returned = time.perf_counter()
    owed_at_return = sorted(rebuilds.owed())
    if not rebuilds.wait_idle(600):
        raise SystemExit("the rebuild queue did not finish")
    finished = time.perf_counter()
    hooks.building.set()          # a re-stamp builds nothing: release a writer still waiting for a build
    thread.join(120)
    if reply.get("status") != "ok":
        raise SystemExit("the change was not applied")
    return {"outcome": reply["payload"]["ack"]["outcome"], "ack_seconds": returned - started,
            "writer_wait_seconds": waits[0] if waits else None, "index_work_seconds": finished - started,
            "owed_when_the_ack_returned": owed_at_return,
            "queue_result": rebuilds.results[-1][0] if rebuilds.results else None}


def review_round(node, mode: str, hooks: Hooks, rebuilds) -> dict:
    """One owner review change's index part, under the gate as the review hooks run it: before N3 the sweep and
    every rebuild; after, the drop of what the guard cannot see and the queue (the builds run after)."""
    from topos.core.handlers import permissions_v2 as handlers
    from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
    from topos.storage.db.write_gate import with_db_write
    waits = []
    thread = writer(hooks, waits)
    token = set_principal(Principal(cls=OWNER_APP, channel="cp_relay", acting_user=node.mc.OWNER_ID))
    try:
        started = time.perf_counter()
        with with_db_write():
            if mode == "before":
                node.index.sweep()
                node.index.rebuild_all()
            else:
                handlers._refresh_message_search(SimpleNamespace(
                    protocol=node.protocol, record_keys_root=lambda: node.index.root, index_rebuilds=lambda: rebuilds))
        held = time.perf_counter() - started
        if rebuilds is not None and not rebuilds.wait_idle(600):
            raise SystemExit("the rebuild queue did not finish")
        finished = time.perf_counter()
    finally:
        reset_principal(token)
    thread.join(120)
    return {"change_gate_hold": held, "writer_wait_seconds": waits[0] if waits else None,
            "index_work_seconds": finished - started}


def measure(mode: str, scratch: Path, hooks: Hooks, gate: GateHolds, *, members: int, rounds: int,
            seed: int) -> dict:
    from topos.permissions_v2 import runtime as runtime_module
    from topos.permissions_v2.index_rebuilds import IndexRebuilds
    from topos.permissions_v2.refresh_loop import protection_sync
    node = build_node(scratch / mode, members=members, seed=seed)
    rebuilds = None
    if mode == "after":
        rebuilds = IndexRebuilds(ledger=node.ledger, root=node.index.root, index=lambda: node.index,
                                 sync_protection=protection_sync(node.protocol))
        runtime = SimpleNamespace(protocol=node.protocol, record_keys_root=lambda: node.index.root,
                                  message_search_index=lambda: node.index, index_rebuilds=lambda: rebuilds)
        runtime_module.get_runtime = lambda: runtime
    results = []
    try:
        for round_number in range(rounds):
            number = (3 + round_number) % len(SHARES)
            change = signed_change(node, number, generation=2 + round_number // len(SHARES), round_number=round_number)
            before = identities(node)
            hooks.reset()
            gate.take()
            result = (before_round(node, change, hooks) if mode == "before"
                      else after_round(node, change, hooks, rebuilds))
            after = identities(node)
            holds = gate.take()
            queue = [seconds for thread, seconds in holds if thread == "p2c-index-rebuilds"]
            result.update(share=SHARES[number], builds=hooks.builds,
                          indexes_rebuilt=sum(before[grant] != after[grant] for grant in SHARES),
                          longest_gate_hold=max(seconds for _thread, seconds in holds),
                          rebuild_gate_holds={"count": len(queue), "longest": max(queue)} if queue else None)
            results.append(result)
            print(json.dumps({"mode": mode, **result}), flush=True)
        # One change of only a share's daily number.
        number = 8
        change = signed_change(node, number, generation=3, round_number="light", light=True)
        before = identities(node)
        hooks.reset()
        gate.take()
        light = (before_round(node, change, hooks) if mode == "before"
                 else after_round(node, change, hooks, rebuilds, patience=0))
        after = identities(node)
        light.update(share=SHARES[number], builds=hooks.builds,
                     indexes_rebuilt=sum(before[grant] != after[grant] for grant in SHARES),
                     longest_gate_hold=max(seconds for _thread, seconds in gate.take()))
        print(json.dumps({"mode": mode, "light": light}), flush=True)
        before = identities(node)
        hooks.reset()
        gate.take()
        review = review_round(node, mode, hooks, rebuilds)
        after = identities(node)
        review.update(builds=hooks.builds, indexes_rebuilt=sum(before[grant] != after[grant] for grant in SHARES),
                      longest_gate_hold=max(seconds for _thread, seconds in gate.take()))
        print(json.dumps({"mode": mode, "review": review}), flush=True)
    finally:
        if rebuilds is not None:
            rebuilds.close()
    return {"rounds": results, "summary": summary(results), "light": light, "review": review}


def summary(results: list) -> dict:
    def spread(key):
        values = [result[key] for result in results if result[key] is not None]
        return {"median": statistics.median(values), "max": max(values)} if values else None
    queue = [result["rebuild_gate_holds"]["longest"] for result in results if result["rebuild_gate_holds"]]
    return {"ack_seconds": spread("ack_seconds"), "writer_wait_seconds": spread("writer_wait_seconds"),
            "index_work_seconds": spread("index_work_seconds"), "longest_gate_hold": spread("longest_gate_hold"),
            "rebuild_gate_hold_longest": max(queue) if queue else None,
            "indexes_rebuilt": sorted({result["indexes_rebuilt"] for result in results}),
            "builds": sorted({result["builds"] for result in results})}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--members", type=int, default=40, help="permitted messages per share")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--embed-ms", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=31)
    args = parser.parse_args()
    import topos
    print(json.dumps({"topos": topos.__file__}), flush=True)
    report = {"shares": len(SHARES), "members": args.members, "rounds": args.rounds, "embed_ms": args.embed_ms}
    hooks, gate = Hooks(args.embed_ms), GateHolds()   # once: the same hooks for both columns
    with tempfile.TemporaryDirectory(prefix="n3-many-shares-") as scratch:
        for mode in ("before", "after"):
            report[mode] = measure(mode, Path(scratch).resolve(), hooks, gate, members=args.members,
                                   rounds=args.rounds, seed=args.seed)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({mode: {"change": report[mode]["summary"], "light": report[mode]["light"],
                             "review": report[mode]["review"]} for mode in ("before", "after")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
