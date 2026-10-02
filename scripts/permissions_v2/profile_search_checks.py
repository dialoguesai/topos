#!/usr/bin/env python3
"""MG-4: what the p2c search checks cost, function by function, on the shared census copy.

Counts and timings only. The node runs these on every search (plan §3):
- check_own = SearchIndexService._current(deep=False): index_load, the recheck's own pass, and the
  send-time check in search_transport.py, so three times per search;
- the 10 s sweep's pass = _current(deep=True), which adds _lineage_fingerprint for every member;
- the recheck's per-candidate qualification (qualify_automatic_message: snapshot, _floors,
  validate_existing, ...);
- EntityBoundary construction, the review store's authority digest and the protection revision.

The engine code runs unedited inside WS1's census_support.copy_session (its docstring names the
three behaviours it holds still so a copy reads as the live node would). Every store is opened
read-only: the canonical copy, the ledger, the index file and the record key (immutable), and the
review digest is computed on a read-only connection to the copy's review store. Nothing is written
under the copy or the key's directory, and the key is never copied or printed.

Output: per-function call counts and milliseconds, per-member distributions and whether the index
is current. Never an id, key, name, path of the owner's data or content.

    export TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env
    PYTHONPATH=<engine worktree> python3 scripts/permissions_v2/profile_search_checks.py \\
        --copy <census-copy/run-id> --census-support <dir with census_support.py> [--keys <keys.db>] [--json out]
"""
from __future__ import annotations

import argparse
import cProfile
import importlib
import json
import logging
import pstats
import sqlite3
import statistics
import sys
import time
from pathlib import Path


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 3)


def _dist(values_ms: list[float]) -> dict:
    if not values_ms:
        return {"n": 0}
    ordered = sorted(values_ms)
    return {"n": len(ordered), "median_ms": round(statistics.median(ordered), 3),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 3),
            "max_ms": round(ordered[-1], 3), "total_ms": round(sum(ordered), 3)}


def _timed(fn, repeat: int) -> list[float]:
    out = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        out.append((time.perf_counter() - started) * 1000)
    return out


class _StaleStages(logging.Handler):
    """search_index logs 'message search index stale (<stage>)'; the stage is a fixed code word."""

    def __init__(self):
        super().__init__()
        self.stages = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("message search index stale ("):
            self.stages.append(message[len("message search index stale ("):-1])


def targets():
    """Code objects -> labels, so profiler rows are matched exactly (never by name alone)."""
    from topos.permissions_v2 import (automatic_message_review, entity_boundary, evidence, knowledge_projections,
                                      message_evidence, protection_clock, reconciliation_provenance, search_index)
    functions = {
        "_current": search_index.SearchIndexService._current,
        "_entity_dependencies_current": search_index.SearchIndexService._entity_dependencies_current,
        "SearchIndexService._open": search_index.SearchIndexService._open,
        "_live_rows": search_index._live_rows,
        "_member_fingerprint": search_index._member_fingerprint,
        "_lineage_fingerprint": search_index._lineage_fingerprint,
        "EntityBoundary.__init__": entity_boundary.EntityBoundary.__init__,
        "EntityBoundary.check": entity_boundary.EntityBoundary.check,
        "context_for": automatic_message_review.context_for,
        "knowledge_projections.current_revision": knowledge_projections.current_revision,
        "qualify_automatic_message": message_evidence.qualify_automatic_message,
        "snapshot_message": message_evidence.snapshot_message,
        "_floors": message_evidence._floors,
        "validate_existing": reconciliation_provenance.validate_existing,
        "_source_sibling_floor": evidence.EvidenceResolver._source_sibling_floor,
        "EvidenceReviewStore._authority_digest": evidence.EvidenceReviewStore._authority_digest,
        "current_protection_revision": protection_clock.current_protection_revision,
    }
    out = {}
    for label, function in functions.items():
        code = getattr(function, "__code__", None) or getattr(getattr(function, "__func__", None), "__code__", None)
        out[(code.co_filename, code.co_firstlineno, code.co_name)] = label
    return out


def profiled(fn, keys: dict) -> dict:
    profile = cProfile.Profile()
    profile.enable()
    try:
        fn()
    finally:
        profile.disable()
    stats = pstats.Stats(profile).stats
    rows = {}
    for key, label in keys.items():
        if key in stats:
            _cc, calls, tottime, cumtime, _callers = stats[key]
            rows[label] = {"calls": calls, "self_ms": _ms(tottime), "cum_ms": _ms(cumtime)}
    return rows


def active_knowledge_grant(ledger_path: Path, now: int, cs):
    from topos.permissions_v2.canonical import PolicyError
    from topos.permissions_v2.ledger import PolicyLedger
    from topos.permissions_v2.search_contract import CAPABILITY_KNOWLEDGE_SEARCH
    conn = cs.ro(ledger_path, immutable=True)
    try:
        ledger = object.__new__(PolicyLedger)  # _authority only reads; the constructor would write
        found = []
        for (grant_id,) in conn.execute("SELECT grant_id FROM p2a_grants ORDER BY grant_id"):
            try:
                authority, policy = PolicyLedger._authority(ledger, conn, grant_id, now)
            except PolicyError:
                continue
            if policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH:
                found.append((grant_id, authority, policy))
    finally:
        conn.close()
    if len(found) != 1:
        raise SystemExit(f"refused: expected one active knowledge-search grant, found {len(found)}")
    return found[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MG-4 function-level profile on the census copy")
    parser.add_argument("--copy", type=Path, required=True)
    parser.add_argument("--census-support", type=Path, required=True)
    parser.add_argument("--keys", type=Path, help="WS1's keys.db, read in place (mode=ro, immutable)")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(args.census_support))
    cs = importlib.import_module("census_support")
    cs.require_scratch_environment()
    copy = cs.refuse_live(args.copy)
    manifest = json.loads((copy / "census-copy-manifest.json").read_text())
    now = int(manifest["copied_at"])
    config = cs.load_config(copy)
    binding = cs.binding_from_config(config)
    canonical = copy / "database.db"
    durable = copy / "permissions-v2"
    review_store = durable / "evidence-reviews.db"

    from topos.permissions_v2.automatic_message_review import context_for
    from topos.permissions_v2.canonical import PolicyError
    from topos.permissions_v2.entity_boundary import EntityBoundary
    from topos.permissions_v2.evidence import EvidenceIdentity, EvidenceResolver, EvidenceReviewStore
    from topos.permissions_v2.knowledge_projections import current_revision
    from topos.permissions_v2.message_evidence import qualify_automatic_message
    from topos.permissions_v2 import protection_clock
    from topos.permissions_v2.protection_clock import clock_state, current_protection_revision
    from topos.permissions_v2.search_contract import CAPABILITY_KNOWLEDGE_SEARCH
    from topos.permissions_v2.search_index import (SearchIndexService, _lineage_fingerprint, _live_rows,
                                                   _member_fingerprint, index_path, unseal)

    grant_id, authority, policy = active_knowledge_grant(durable / "ledger.db", now, cs)
    index_root = durable / "message-search"
    path = index_path(index_root, grant_id)
    key = None
    if args.keys is not None:
        cs.refuse_live(args.keys)
        kconn = cs.ro(args.keys, immutable=True)
        try:
            row = kconn.execute("SELECT key FROM p2c_record_keys WHERE grant_id=?", (grant_id,)).fetchone()
            key = row[0] if row else None
        finally:
            kconn.close()
    report = {"schema": "MG-4-profile/v1", "copy_run_id": manifest.get("run_id"), "repeat": args.repeat,
              "capability_is_knowledge_search": policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH,
              "key_available": key is not None, "timings": {}, "functions": {}, "notes": []}
    keys = targets()
    stale = _StaleStages()
    logging.getLogger("topos.permissions_v2.search_index").addHandler(stale)

    def review_digest_once():
        db = cs.ro(review_store, immutable=True)
        db.row_factory = None  # as the store's own connection (evidence.py _db)
        try:
            db.execute("BEGIN")
            return EvidenceReviewStore._authority_digest(db)
        finally:
            db.close()

    class Reviews:
        """current_authority_digest as the node computes it under its rollback floor: the digest twice (evidence.py _db)."""
        def __init__(self):
            self.binding = binding

        def current_authority_digest(self):
            review_digest_once()
            return review_digest_once()

    class Keys:
        def get(self, wanted, create=False):
            return key if wanted == grant_id else None

    service = object.__new__(SearchIndexService)
    service.ledger, service.reviews, service.root, service.keys = None, Reviews(), index_root, Keys()

    with cs.copy_session(canonical, manifest["live_canonical_path"]) as session:
        service.resolver = resolver = EvidenceResolver(canonical, binding=binding)
        opened = time.perf_counter()
        with resolver._read(gated=False) as (conn, floor):
            report["timings"]["resolver_read_open_ms"] = _ms(time.perf_counter() - opened)
            clock = clock_state(conn)
            T = report["timings"]
            T["clock_state"] = _dist(_timed(lambda: clock_state(conn), args.repeat))
            T["entity_boundary_init"] = _dist(_timed(lambda: EntityBoundary(conn), args.repeat))
            T["review_digest_once"] = _dist(_timed(review_digest_once, args.repeat))

            def protection_cold():
                with protection_clock._REVISIONS_LOCK:  # this process's fold cache; the node keeps it warm per generation
                    protection_clock._REVISIONS.clear()
                current_protection_revision(conn, owner_id=binding.owner_id)
            T["protection_revision_cold"] = _dist(_timed(protection_cold, args.repeat))
            T["protection_revision_warm"] = _dist(_timed(
                lambda: current_protection_revision(conn, owner_id=binding.owner_id), args.repeat))
            T["index_open"] = _dist(_timed(lambda: SearchIndexService._open(path), args.repeat))
            T["index_load"] = _dist(_timed(lambda: service.load(grant_id, authority), args.repeat))
            report["functions"]["entity_boundary_init"] = profiled(lambda: EntityBoundary(conn), keys)

            for deep in (False, True):
                label = "current_deep" if deep else "current_check_own"
                stale.stages.clear()
                result = service._current(path, grant_id, authority, clock, conn, deep=deep)
                report[f"{label}_result"] = {"current": bool(result), "stale_stage": stale.stages[-1] if stale.stages else None}
                repeat = args.repeat if not deep else max(1, min(args.repeat, 2))  # deep scans every member's copies
                T[label] = _dist(_timed(lambda: service._current(path, grant_id, authority, clock, conn, deep=deep),
                                        repeat))
                report["functions"][label] = profiled(
                    lambda: service._current(path, grant_id, authority, clock, conn, deep=deep), keys)

            if key is None:
                report["notes"].append("no key: the per-member loop and the recheck qualification were not run")
            else:
                index = SearchIndexService._open(path)
                boundary = EntityBoundary(conn)
                checked = {}
                per = {name: [] for name in ("dependencies", "live_rows", "context", "projection", "boundary_check",
                                             "member_fingerprint", "lineage_fingerprint", "qualify_recheck")}
                knowledge = authority.capability_version == CAPABILITY_KNOWLEDGE_SEARCH
                rdb = cs.ro(review_store, immutable=True)
                rdb.row_factory = None
                try:
                    frozen = EvidenceReviewStore.freeze(EvidenceReviewStore, rdb)
                finally:
                    rdb.close()
                outcomes = {"members": 0, "qualified": 0, "refused": 0, "projection_members": 0}
                for opaque, sealed in index["sealed"]:
                    member = unseal(key, opaque, sealed)
                    outcomes["members"] += 1
                    clock_start = time.perf_counter()
                    service._entity_dependencies_current(conn, boundary, member.get("entity_dependencies"), checked)
                    t1 = time.perf_counter()
                    rows, facts = _live_rows(conn, member)
                    t2 = time.perf_counter()
                    identity = EvidenceIdentity.parse(member["message"]) if member.get("message") else None
                    if knowledge and identity is not None and len(rows) == 1:
                        context_for(conn, identity, dict(rows[0]), boundary=boundary)
                    t3 = time.perf_counter()
                    if member.get("projection"):
                        outcomes["projection_members"] += 1
                        projection = member["projection"]
                        # As _members_current runs it: with the boundary, Off-limits on the current rows too.
                        current_revision(conn, projection["table"], projection["record_id"], boundary=boundary)
                    t4 = time.perf_counter()
                    if len(rows) == 1:
                        boundary.check(table=member["table"], record_id=member["record_id"], source_id=member["source_id"],
                                       dataset_id=member["dataset_id"], row=dict(rows[0]))
                    t5 = time.perf_counter()
                    _member_fingerprint(rows, facts, table=member["table"])
                    t6 = time.perf_counter()
                    _lineage_fingerprint(conn, member, dict(rows[0]).get("content") if len(rows) == 1 else None)
                    t7 = time.perf_counter()
                    for name, (a, b) in {"dependencies": (clock_start, t1), "live_rows": (t1, t2), "context": (t2, t3),
                                         "projection": (t3, t4), "boundary_check": (t4, t5),
                                         "member_fingerprint": (t5, t6), "lineage_fingerprint": (t6, t7)}.items():
                        per[name].append((b - a) * 1000)
                    if identity is not None:
                        started = time.perf_counter()
                        try:
                            qualify_automatic_message(resolver, conn, floor, identity, frozen, None)
                            outcomes["qualified"] += 1
                        except PolicyError:
                            outcomes["refused"] += 1
                        per["qualify_recheck"].append((time.perf_counter() - started) * 1000)
                report["per_member"] = {name: _dist(values) for name, values in per.items()}
                report["member_outcomes"] = outcomes

                def qualify_all():
                    for opaque, sealed in index["sealed"]:
                        member = unseal(key, opaque, sealed)
                        if member.get("message"):
                            try:
                                qualify_automatic_message(resolver, conn, floor, EvidenceIdentity.parse(member["message"]),
                                                          frozen, None)
                            except PolicyError:
                                pass
                report["functions"]["qualify_recheck_all_members"] = profiled(qualify_all, keys)
        report["session"] = {"aliased_revisions": session.aliased_revisions,
                             "lineage_key_completions_skipped": session.lineage_key_completions_skipped,
                             "ingest_marker_publishes_held_in_memory": session.ingest_marker_publishes_held_in_memory}

    text = json.dumps(report, indent=1, sort_keys=True)
    if args.json:
        args.json.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
