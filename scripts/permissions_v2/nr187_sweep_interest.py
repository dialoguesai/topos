#!/usr/bin/env python3
"""NR187: the daemon sweep's interest step with `SWEEP_INTEREST_VERDICT` off and on, on invented browsing.

Two things, both on one invented node per arm (nothing of the owner's is read):

- `measure`: CPU per quiet sweep (thread time, the sweep runs on the calling thread) with the switch off and on, and
  the part of it spent deciding interest members (`_interests_current`). With `--messages N` it also times the sweep
  of the load harness's message share of N members (`request_load_harness.build_fixture`), the per-sweep cost that is
  left once the interest step is kept (R14).
- `equivalence`: a scripted sequence of sweeps with changes in between (quiet sweeps, unrelated commits, a touched
  index file, switch flips, a later visit, a new person, an Off-limits entry, a relabel, a reassessment, an excluded
  visit, an edited visit, a revoked receipt, and the rebuilds and assessments they owe). Every sweep's decision (how
  many indexes it removed, whether the index is there, the stale stages it logged, and the interest step's verdict)
  is written per arm; the arms must agree step for step. `--arms off` runs only the off arm, so the same script run
  against another engine tree (`NR187_ENGINE_ROOT`, e.g. an export of main) gives main's decisions to compare.

The fixture: `--clusters` topic clusters with invented labels over four months (June to September 2026; the grant's
90-day window holds July to September), `--visits` visits per cluster-month on four days, a person universe of
`--persons` invented names (two aliases each; some mentioned by visits), Off-limits entries and excluded entities that
touch no label, two clusters whose own label names their site and a second label stands in, every label assessed.

Run from the engine root with TOPOS_KEY=synthetic and scratch TOPOS_DATABASE_PATH, TOPOS_ENV_FILE and HOME:
    <topos venv python> scripts/permissions_v2/nr187_sweep_interest.py measure --out <dir>
    <topos venv python> scripts/permissions_v2/nr187_sweep_interest.py equivalence --out <dir>
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("NR187_ENGINE_ROOT") or Path(__file__).resolve().parents[2]).resolve()
sys.path.insert(0, str(ROOT))

SWITCH = "TOPOS_PERMISSIONS_V2_SWEEP_INTEREST_VERDICT"
FAMILY = "TOPOS_PERMISSIONS_V2_INTEREST_SOURCES"
RELABEL = "TOPOS_PERMISSIONS_V2_INTEREST_RELABEL"
TOPICS = ("sourdough", "woodworking", "astronomy", "birdwatching", "ceramics", "orienteering", "beekeeping", "kayaking",
          "calligraphy", "mycology", "bookbinding", "falconry", "origami", "glassblowing", "archery", "fermentation",
          "botany", "geology", "cartography", "knitting", "pottery", "sailing", "climbing", "cycling", "chess",
          "gardening", "photography", "baking", "carpentry", "weaving")
FACETS = ("techniques", "history", "gear", "basics", "projects", "tools", "events", "guides", "journals", "notes")
SYLLABLES = ("bra", "tel", "vor", "qui", "zan", "mol", "dri", "kes", "tov", "lun", "ser", "pha", "gor", "wyn", "ixa",
             "rul", "ost", "mer", "dax", "fen", "jul", "kor", "nim", "pel", "rho", "sab", "tum", "vex", "yor", "zil")


def _name(rng) -> str:
    return " ".join("".join(rng.choice(SYLLABLES) for _ in range(rng.choice((2, 3)))).capitalize() for _ in range(2))


def build_node(root: Path, *, clusters: int, visits: int, persons: int, seed: int):
    """An invented node: (canonical path, Node with its index built, labels by cluster)."""
    from tests.permissions_v2.interest_fixtures import at, cluster, install, open_db, visit
    from tests.permissions_v2 import test_interest_door as door
    from topos.permissions_v2.protection_clock import ensure_protection_clock

    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "canonical.db"
    conn = open_db(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("UPDATE engine_config SET value=? WHERE key='user_id'", (door.OWNER,))
    install(conn, user=door.OWNER, dataset=door.DATASET)
    conn.commit()
    conn.close()
    ensure_protection_clock(path, owner_id=door.OWNER)
    labels = {}
    with door._db(path) as conn:
        door._attest(conn)
        n = 0
        mentioned = []
        for c in range(clusters):
            cluster_id = f"tc_{c:03d}"
            site_label = c < 2
            label = (f"pagehost{c} / {TOPICS[c % len(TOPICS)]}" if site_label
                     else f"{TOPICS[c % len(TOPICS)]} {FACETS[(c // len(TOPICS)) % len(FACETS)]}")
            labels[cluster_id] = label
            cluster(conn, cluster_id, label)
            for month in (6, 7, 8, 9):
                days = (2, 6, 11, 15) if month == 9 else (3, 9, 17, 24)
                for i in range(visits):
                    event = visit(conn, n, at(month, days[i % 4], hour=8 + i % 10), cluster_id=cluster_id,
                                  dataset=door.DATASET, host=f"pagehost{c}.example",
                                  title=f"Invented page {n} on {TOPICS[c % len(TOPICS)]}")
                    if rng.random() < 0.05:
                        mentioned.append(event)
                    n += 1
        for i in range(visits * 4):                        # browsing that no cluster holds
            visit(conn, n, at(8, 1 + i % 28), cluster_id=None, dataset=door.DATASET, host="elsewhere.example")
            n += 1
        names = set()
        for p in range(persons):
            name = _name(rng)
            while name in names:
                name = _name(rng)
            names.add(name)
            aliases = [name.split()[0], _name(rng)]
            conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json) "
                         "VALUES (?,?,?,?,?)", (f"p-{p}", "person", name, name.lower(), json.dumps(aliases)))
        for i, event in enumerate(mentioned):
            conn.execute("INSERT INTO entity_mentions (mention_id, entity_id, record_id, canonical_table) "
                         "VALUES (?,?,?,'activity_events')", (f"mn-{i}", f"p-{rng.randrange(persons)}", event))
        for i in range(3):
            name = _name(rng)
            conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
                         "rebuild_state) VALUES (?, '', ?, ?, 'complete')", (f"bh-{i}", name, name.lower()))
        for i in range(5):
            name = _name(rng) + " Guild"
            conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) "
                         "VALUES (?,?,?,?)", (f"x-{i}", "organization", name, name.lower()))
            conn.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
                         "VALUES (?,?,?)", (f"xe-{i}", "entity", f"x-{i}"))
    door._second_label(path, "outdoor pursuits reviews")
    door._assess(path)
    return path, labels


def make_node(path: Path, root: Path):
    from tests.permissions_v2 import test_interest_door as door

    class Patch:   # the door tests' `_node` takes pytest's monkeypatch; one setattr is all it uses
        def setattr(self, target, name, value):
            setattr(target, name, value)
    node = door._node(path, root, Patch())
    built = door._rebuild(node)
    return node, built


class Recorder:
    """The interest step's verdict per sweep, the full decisions and their thread time, and the stale stages logged."""

    def __init__(self):
        from topos.permissions_v2.search_index import SearchIndexService
        self.local = threading.local()
        self.verdicts, self.stages = [], []
        self.full, self.full_seconds = 0, 0.0
        real = SearchIndexService._interests_current
        recorder = self

        def full(service, conn, sealed, policy, boundary, opt_outs=frozenset()):
            started = time.thread_time()
            try:
                verdict = real(service, conn, sealed, policy, boundary, opt_outs)
            finally:
                if policy is None:
                    recorder.full_seconds += time.thread_time() - started
            if policy is None:
                recorder.full += 1
                if not getattr(recorder.local, "kept", False):
                    recorder.verdicts.append(verdict)
            return verdict
        SearchIndexService._interests_current = full
        kept_real = getattr(SearchIndexService, "_interests_current_kept", None)
        if kept_real is not None:
            def kept(service, *args, **kwargs):
                recorder.local.kept = True
                try:
                    verdict = kept_real(service, *args, **kwargs)
                finally:
                    recorder.local.kept = False
                recorder.verdicts.append(verdict)
                return verdict
            SearchIndexService._interests_current_kept = kept

        class Catch(logging.Handler):
            def emit(self, record):
                match = re.fullmatch(r"message search index stale \((\w+)\)", record.getMessage())
                if match:
                    recorder.stages.append(match.group(1))
        logging.getLogger("topos.permissions_v2.search_index").addHandler(Catch())


def sweep(node, recorder, on: bool) -> dict:
    from tests.permissions_v2.message_search_harness import owner
    from topos.permissions_v2.search_index import index_path
    os.environ[SWITCH] = "true" if on else "false"
    recorder.verdicts.clear()
    recorder.stages.clear()
    full_before, seconds_before = recorder.full, recorder.full_seconds
    started, wall = time.thread_time(), time.perf_counter()
    with owner():
        removed = node.index.sweep(now=node.now[0])
    cpu, wall = time.thread_time() - started, time.perf_counter() - wall
    file = index_path(node.index.root, "grant-search")
    return {"removed": removed, "present": file.exists(), "stages": list(recorder.stages),
            "verdicts": list(recorder.verdicts), "full": recorder.full - full_before,
            "cpu_s": round(cpu, 4), "wall_s": round(wall, 4),
            "interest_cpu_s": round(recorder.full_seconds - seconds_before, 4)}


# --- the scripted sequence -----------------------------------------------------------------------------------------

def change(path: Path, node, labels: dict, name: str) -> None:
    from tests.permissions_v2 import test_interest_door as door
    from tests.permissions_v2.interest_fixtures import at, cluster, visit
    from topos.permissions_v2 import capture_receipts as cr
    from topos.permissions_v2.search_index import index_path
    if name == "touch_index_file":
        file = index_path(node.index.root, "grant-search")
        info = file.stat()
        os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        return
    if name == "second_label_switch_off":
        os.environ[RELABEL] = "off"
        return
    if name == "second_label_switch_on":
        os.environ.pop(RELABEL, None)
        return
    if name == "assess":
        door._assess(path)
        return
    if name == "reassess_special":
        door._assess(path, {"domains": ["hobbies"], "sensitivity": "special", "protected_content": "none"})
        return
    if name == "reassess_ordinary":
        door._assess(path)
        return
    with door._db(path) as conn:
        if name == "unrelated_commit":
            conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('nr187', ?)", (str(time.time()),))
        elif name == "unclustered_visit":
            visit(conn, 900_000, at(9, 18), cluster_id=None, dataset=door.DATASET, host="elsewhere.example")
        elif name == "visit_after_the_build":
            visit(conn, 900_001, "2026-09-20T12:05:00.000Z", cluster_id="tc_005", dataset=door.DATASET,
                  host="pagehost5.example")
        elif name == "new_person_named_by_no_label":
            conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) "
                         "VALUES ('p-new','person','Vexilla Thornquist','vexilla thornquist')")
        elif name == "off_limits_entry":
            conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
                         "rebuild_state) VALUES ('bh-new','','Ostrava Quimby','ostrava quimby','complete')")
        elif name == "relabel":
            cluster(conn, "tc_007", labels["tc_007"] + " archive")
        elif name == "excluded_visit":
            conn.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
                         "VALUES ('xv-1','record','browser:v30')")
        elif name == "edited_visit":
            conn.execute("UPDATE activity_events SET url='https://pagehost9.example/moved' WHERE event_id=(SELECT "
                         "a.event_id FROM activity_events a JOIN topic_cluster_members m ON m.record_id=a.event_id "
                         "WHERE m.cluster_id='tc_009' AND a.occurred_at LIKE '2026-08%' ORDER BY a.event_id LIMIT 1)")
        elif name == "receipt_revoked":
            receipt = cr.receipts(conn, owner_id=door.OWNER)[0]["receipt_id"]
            cr.revoke(conn, owner_id=door.OWNER, receipt_id=receipt, now=1_700_000_500)
        else:
            raise SystemExit(f"unknown change {name}")


SEQUENCE = (["sweep"] * 3 + ["change:unrelated_commit", "sweep", "change:unclustered_visit", "sweep",
            "change:visit_after_the_build", "sweep", "sweep", "change:touch_index_file", "sweep",
            "change:second_label_switch_off", "sweep", "change:second_label_switch_on", "rebuild", "sweep", "sweep",
            "change:new_person_named_by_no_label", "sweep", "sweep",
            "change:off_limits_entry", "sweep", "rebuild", "change:assess", "rebuild", "sweep", "sweep",
            "change:relabel", "sweep", "rebuild", "sweep", "change:assess", "rebuild", "sweep", "sweep",
            "change:reassess_special", "sweep", "rebuild", "sweep", "change:reassess_ordinary", "rebuild", "sweep",
            "change:excluded_visit", "sweep", "rebuild", "sweep", "sweep", "change:edited_visit", "sweep", "rebuild",
            "sweep", "sweep", "change:receipt_revoked", "sweep", "rebuild", "sweep"] + ["sweep"] * 3)


def run_arm(scratch: Path, args, on: bool) -> dict:
    from tests.permissions_v2 import test_interest_door as door
    os.environ[FAMILY] = "true"
    os.environ.pop(RELABEL, None)
    arm_root = scratch / ("on" if on else "off")
    path, labels = build_node(arm_root / "node", clusters=args.clusters, visits=args.visits, persons=args.persons,
                              seed=args.seed)
    node, built = make_node(path, arm_root)
    recorder = Recorder()
    steps = []
    for step in SEQUENCE:
        if step == "sweep":
            steps.append({"step": "sweep", **sweep(node, recorder, on)})
        elif step == "rebuild":
            with node.ledger._transaction() as conn:   # as the refresh loop's restore does first (protection sync)
                node.protocol._sync_protection(conn)
            steps.append({"step": "rebuild", **door._rebuild(node)})
        else:
            change(path, node, labels, step.split(":", 1)[1])
            steps.append({"step": step})
    return {"built": built, "steps": steps}


def decisions(arm: dict) -> list:
    """What each step decided: never the timings, never how a verdict was reached."""
    out = []
    for step in arm["steps"]:
        if step["step"] == "sweep":
            out.append(["sweep", step["removed"], step["present"], step["stages"], step["verdicts"]])
        elif step["step"] == "rebuild":
            out.append(["rebuild", step.get("state"), step.get("member_count")])
        else:
            out.append([step["step"]])
    return out


def equivalence(args) -> int:
    import topos
    scratch = Path(args.scratch or tempfile.mkdtemp(prefix="nr187-eq-"))
    arms = {"on": True, "off": False} if args.arms == "both" else {args.arms: args.arms == "on"}
    result = {"engine": str(Path(topos.__file__).resolve().parent), "fixture": vars(args) | {"out": str(args.out)},
              "arms": {}}
    for name, on in arms.items():
        result["arms"][name] = run_arm(scratch, args, on)
    for name, arm in result["arms"].items():
        sweeps = [step for step in arm["steps"] if step["step"] == "sweep"]
        arm["summary"] = {"sweeps": len(sweeps), "removed": sum(step["removed"] for step in sweeps),
                          "with_interest_step": sum(bool(step["verdicts"]) for step in sweeps),
                          "verdicts_true": sum(v is True for step in sweeps for v in step["verdicts"]),
                          "verdicts_false": sum(v is False for step in sweeps for v in step["verdicts"]),
                          "full_decisions": sum(step["full"] for step in sweeps),
                          "decisions_digest": __import__("hashlib").sha256(
                              json.dumps(decisions(arm)).encode()).hexdigest()}
    if len(result["arms"]) == 2:
        on, off = (decisions(result["arms"][name]) for name in ("on", "off"))
        result["equal"] = on == off
        result["differences"] = [[i, a, b] for i, (a, b) in enumerate(zip(on, off)) if a != b]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"equivalence-{args.arms}.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({"engine": result["engine"], "equal": result.get("equal"),
                      **{name: arm["summary"] for name, arm in result["arms"].items()}}, indent=1))
    return 0 if result.get("equal", True) else 1


# --- CPU per sweep ---------------------------------------------------------------------------------------------------

def measure(args) -> int:
    import topos
    scratch = Path(args.scratch or tempfile.mkdtemp(prefix="nr187-cpu-"))
    os.environ[FAMILY] = "true"
    out = {"engine": str(Path(topos.__file__).resolve().parent), "fixture": vars(args) | {"out": str(args.out)},
           "load": os.getloadavg(), "arms": {}}
    path, _labels = build_node(scratch / "node", clusters=args.clusters, visits=args.visits, persons=args.persons,
                               seed=args.seed)
    node, built = make_node(path, scratch)
    out["built"] = built
    recorder = Recorder()
    for round_ in range(args.rounds):                      # interleaved, so load drifts hit both arms alike
        for name, on in (("off", False), ("on", True)):
            for _ in range(args.sweeps):
                out["arms"].setdefault(name, []).append(sweep(node, recorder, on))
    for name, rows in out["arms"].items():
        steady = [row for row in rows if row["full"] == 0] if name == "on" else rows
        out[f"{name}_summary"] = {
            "sweeps": len(rows), "present": all(row["present"] for row in rows),
            "cpu_s_median": statistics.median(row["cpu_s"] for row in rows),
            "cpu_s_median_steady": statistics.median(row["cpu_s"] for row in steady) if steady else None,
            "interest_cpu_s_median": statistics.median(row["interest_cpu_s"] for row in rows),
            "wall_s_median": statistics.median(row["wall_s"] for row in rows),
            "full_decisions": sum(row["full"] for row in rows)}
    if args.messages:
        sys.path.insert(1, str(ROOT / "scripts" / "permissions_v2"))
        import request_load_harness as harness
        os.environ[SWITCH] = "false"
        fixture = harness.build_fixture(scratch / "messages", members=args.messages, spare=0, seed=args.seed,
                                        universe=args.universe)
        from tests.permissions_v2.message_search_harness import owner
        rows = []
        for _ in range(args.sweeps * args.rounds):
            started, wall = time.thread_time(), time.perf_counter()
            with owner():
                removed = fixture.node.index.sweep()
            rows.append({"removed": removed, "cpu_s": round(time.thread_time() - started, 4),
                         "wall_s": round(time.perf_counter() - wall, 4)})
        out["messages"] = {"members": args.messages, "universe": args.universe, "sweeps": rows,
                           "cpu_s_median": statistics.median(row["cpu_s"] for row in rows),
                           "wall_s_median": statistics.median(row["wall_s"] for row in rows),
                           "removed": sum(row["removed"] for row in rows)}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "measure.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({key: value for key, value in out.items() if key.endswith("summary") or key in
                      ("built", "load", "engine")} | ({"messages": {k: v for k, v in out["messages"].items()
                                                                    if k != "sweeps"}} if args.messages else {}),
                     indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("measure", "equivalence"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scratch")
    parser.add_argument("--clusters", type=int, default=40)
    parser.add_argument("--visits", type=int, default=12, help="visits per cluster-month")
    parser.add_argument("--persons", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=187)
    parser.add_argument("--sweeps", type=int, default=4, help="sweeps per arm per round (measure)")
    parser.add_argument("--rounds", type=int, default=3, help="interleaved rounds (measure)")
    parser.add_argument("--messages", type=int, default=0, help="also time a message share of N members (measure)")
    parser.add_argument("--universe", type=int, default=10, help="the message fixture's identity universe")
    parser.add_argument("--arms", choices=("both", "on", "off"), default="both")
    args = parser.parse_args()
    if args.command == "equivalence" and args.clusters < 10:
        parser.error("the sequence changes clusters tc_005, tc_007 and tc_009: --clusters must be 10 or more")
    return measure(args) if args.command == "measure" else equivalence(args)


if __name__ == "__main__":
    raise SystemExit(main())
