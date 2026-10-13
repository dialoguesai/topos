#!/usr/bin/env python3
"""NR166 R0: load both sharing doors at once, in one process, on invented rows, and say where the time goes.

The September benchmarks time one asker. On 13 Oct 2026 the owner's node dropped requests because sharing reads held
the process-wide write gate for seconds while ingestion and other askers waited behind it; nothing reproduced that
on purpose. This does, with no node, no port and no model:

- a fixture of `--members` recovered, machine-assessed messages under one knowledge share
  (`tests/permissions_v2/direct_search_twins.py`, with an active Off-limits boundary), built on the wall clock so
  the real sweep and the real transport run on it unpatched, its canonical database in WAL mode as a node's is.
  Around it, as the September benchmarks wrote one, an identity universe no member touches: per `--universe` unit
  300 people, 150 contacts with two addresses each, 30 merge tombstones, 900 mentions and 1,000 hidden facts,
  so the Off-limits boundary reads a spine of some size (default 10 units; 0 writes none);
- N asker threads, closed loop, through one door per cell: `answers` is the answers door's retrieval
  (`MessageSearchRelease.retrieve_for_answer`, behind the adapter the runtime would hand out); `batch` is the
  own-AI batch (`search_transport.dispatch_message_search_batch`: `_decide_batch`, then the send check);
- beside them a writer that commits to the canonical database under the gate, as ingestion's queue claim does;
  the index sweep on the daemon's interval; and two owner-side probes sampled throughout, a plain read and a small
  gated write;
- one reference pass per fixture with timing and attribution OFF. Every measured request's released bytes are
  compared with it, so a run also shows that attribution changes no answer.

Cells per fixture size: an `idle` pair (one asker, no writer, no sweep), the grid (each `--askers` value, writer and
sweep on) and a `reviews` pair (one asker, and a second writer that publishes machine assessments of items outside
the share, which moves the review store as an assessment catch-up does).

What a cell reports comes from the node's own attribution lines (`topos.permissions_v2.search_timing`, switched on
here), read back with `search_timing_attribution.load_attribution`: each request's own gate hold and wait, every
gate section by call site and caller for the whole process, each stage's time, and the hits and misses of the
"nothing changed" fast paths with the token part that moved.

Two things here are not the node's (the report repeats them):
- N askers stand for N shares of one shape. The transport allows one batch in flight per share; with a single
  share built, that lock is replaced by a fresh one per request so askers meet at the write gate, as askers on
  different shares do. `--one-share` keeps the real lock.
- Askers are plain threads, not the node's inbound queue (16 at once, 128 waiting): queueing and drops at that door
  are not measured. A request slower than the door's budget (`--batch-budget`, `--answers-budget`) is counted.

Everything is written under `--scratch` (a fresh temporary directory by default). Run from the engine root with
scratch TOPOS_DATABASE_PATH and TOPOS_ENV_FILE:
    <topos venv python> scripts/permissions_v2/request_load_harness.py --out <dir>
    <topos venv python> scripts/permissions_v2/request_load_harness.py --compare <dir1>/report.json <dir2>/report.json
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(Path(__file__).resolve().parent))
os.environ.setdefault("TOPOS_KEY", "synthetic-load-harness-key")

TIMINGS, ATTRIBUTION = "TOPOS_PERMISSIONS_V2_SEARCH_TIMINGS", "TOPOS_PERMISSIONS_V2_SEARCH_ATTRIBUTION"
OUTSIDE = "TOPOS_PERMISSIONS_V2_DECIDE_OUTSIDE_GATE"   # NR166 R1; `--decide-outside-gate` switches it on for the cells
DOORS = ("answers", "batch")
#: Stages whose per-request time is reported (a batch's per-query stages are added up per request).
STAGES = {"answers": ("runtime_setup", "answer_index_load", "answer_rank", "answer_recheck", "answer_walk"),
          "batch": ("pre_adapter", "runtime_setup", "admit", "index_load", "embed", "rank", "recheck", "checkpoint",
                    "sign", "send_check", "send", "transport_total")}
PARTS = ("check_own", "load", "boundary", "digest", "members", "dependencies", "dependency_boundary",
         "provenance_setup", "provenance_check", "provenance_snapshot", "open", "token", "protection", "authority",
         "commit")
MATCH_RATIO, MATCH_FLOOR_MS = 1.25, 50.0   # --compare: two runs match when every cell's p50s are within both


def percentile(values, share: float):
    """The nearest-rank percentile; None for no samples."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))]


def spread(values) -> dict:
    return {"n": len(values), "p50": percentile(values, 0.50), "p95": percentile(values, 0.95),
            "max": max(values) if values else None}


class Lines(logging.Handler):
    """The node's timing lines, kept in memory; nothing else is read from any log."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.kept: list[str] = []

    def emit(self, record):
        self.kept.append(record.getMessage())

    def take(self) -> list[str]:
        kept, self.kept = self.kept, []
        return kept


class SlowSections(logging.Handler):
    """How many times the write gate logged a slow section (held or waited 5 s or more)."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record):
        if "slow section" in str(record.msg):
            self.count += 1


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, value):
        self.sent.append(value)


class Refused(Exception):
    pass


# -- the fixture -----------------------------------------------------------------------------------------------------

SYLLABLES = ("ka", "ro", "vi", "ten", "mar", "lo", "su", "dre", "pa", "qui", "nor", "el", "bas", "tu", "fen", "ix")


def write_universe(conn, *, scale: int, seed: int, dataset: str) -> dict:
    """People, contacts, merges and mentions that no member of the share touches (benchmark_n3a_ws4's universe,
    without its protected person: the twins' own `protect` writes that one). Invented names from syllables."""
    import random
    from topos.storage.canonical.conversations_tables import ensure_contact_identifiers_table, ensure_contacts_table
    ensure_contacts_table(conn)
    ensure_contact_identifiers_table(conn)
    rng = random.Random(seed)

    def name() -> str:
        return " ".join("".join(rng.choice(SYLLABLES) for _ in range(rng.randint(2, 3))).capitalize() for _ in range(2))
    entities, contacts = 300 * scale, 150 * scale
    conn.executemany("INSERT INTO contacts(contact_id,dataset_id,source_id,display_name) VALUES(?,?,?,?)",
                     [(f"contact-{i}", dataset, "address_book", name()) for i in range(contacts)])
    conn.executemany("INSERT INTO contact_identifiers(contact_id,dataset_id,source_id,identifier,identifier_type) "
                     "VALUES(?,?,?,?,?)", [row for i in range(contacts) for row in (
                         (f"contact-{i}", dataset, "address_book", f"person{i}@example.test", "email"),
                         (f"contact-{i}", dataset, "address_book", f"other{i}@example.test", "email"))])
    people = []
    for i in range(entities):
        full = name()
        people.append((f"entity-{i}", "person", full, full.lower(), json.dumps([full.split()[0]]),
                       f"contact-{i // 2}" if i % 2 == 0 else None))
    conn.executemany("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,aliases_json,contact_id) "
                     "VALUES(?,?,?,?,?,?)", people)
    conn.executemany("INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into,canonical_name,aliases_json) "
                     "VALUES(?,?,?,?)", [(f"absorbed-{i}", f"entity-{i}", name(), "[]") for i in range(entities // 10)])
    conn.executemany("INSERT INTO entity_mentions(mention_id,entity_id,record_id,source_id,canonical_table,surface_text) "
                     "VALUES(?,?,?,?,?,?)", [(f"mention-{i}", f"entity-{i % entities}", f"elsewhere-{i}", "imessage",
                                              "conversation_messages", people[i % entities][2])
                                             for i in range(3 * entities)])
    return {"entities": entities, "contacts": contacts, "identifiers": 2 * contacts, "merge_tombstones": entities // 10,
            "mentions": 3 * entities, "hidden_facts": 1000 * scale}


def build_fixture(root: Path, *, members: int, spare: int, seed: int, universe: int = 0):
    """A ready share over `members` messages, plus `spare` messages left unassessed for the reviews writer."""
    from tests.permissions_v2 import direct_search_twins as dst
    prepare, publish, parse, add_entity = dst.prepare, dst.publish, dst.parse_assessment, dst.add_entity
    seen = [0]
    written = {}

    def entity_then_universe(conn, entity_id, **kwargs):
        add_entity(conn, entity_id, **kwargs)
        if universe and not written:   # before any assessment, so the index is built over it
            written.update(write_universe(conn, scale=universe, seed=seed, dataset=dst.DATASET))

    def some_prepare(*args, **kwargs):
        seen[0] += 1
        if seen[0] > members:   # a spare: no assessment, so it is not in the share
            return {"spare": True, "snapshot": SimpleNamespace(message=None)}
        return prepare(*args, **kwargs)

    def some_parse(raw, message):
        return None if message is None else parse(raw, message)

    def some_publish(resolver, reviews, prepared, *args, **kwargs):
        if not prepared.get("spare"):
            return publish(resolver, reviews, prepared, *args, **kwargs)
    dst.prepare, dst.publish, dst.parse_assessment, dst.add_entity = (some_prepare, some_publish, some_parse,
                                                                      entity_then_universe)
    try:
        node = dst.build(root, members=members + spare, hidden_facts=1000 * universe, seed=seed, protected=True,
                         now_override=int(time.time()), current_events=True)
    finally:
        dst.prepare, dst.publish, dst.parse_assessment, dst.add_entity = prepare, publish, parse, add_entity
    canonical = Path(node.corpus.resolver.path)
    conn = sqlite3.connect(str(canonical))
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]   # as connection_tuning sets a node's
        conn.execute("CREATE TABLE IF NOT EXISTS nr166_ingest(n INTEGER)")
        conn.commit()
    finally:
        conn.close()
    if str(mode).lower() != "wal":
        raise SystemExit("the fixture's canonical database did not enter WAL mode")
    grant_id = node.search_raw["binding"]["grant_id"]
    count = node.index._open(_index_path(node, grant_id))["count"]
    if count != members:
        raise SystemExit(f"the share holds {count} members, not {members}")
    return SimpleNamespace(node=node, members=members, spare=list(node.corpus.units[members:]), grant_id=grant_id,
                           canonical=canonical, root=root, universe=written,
                           queries=list(dict.fromkeys(dst.queries(members, seed, 60))))


def _index_path(node, grant_id):
    from topos.permissions_v2.search_index import index_path
    return index_path(node.index.root, grant_id)


class Served:
    """What `runtime.get_runtime()` hands the transport: the protocol, and a fresh adapter per request."""

    def __init__(self, node):
        self.node, self.protocol = node, node.protocol

    def _index(self):
        # runtime.message_search_index(): the write gate, entered only to hand back the one index service.
        from topos.storage.db.write_gate import with_db_write
        with with_db_write():
            return self.node.index

    def message_search(self):
        """runtime.message_search(): timing's own gate entry around the index lookup, then a fresh adapter."""
        from tests.permissions_v2.message_search_harness import fake_embedder
        from topos.permissions_v2.search_release import MessageSearchRelease
        from topos.permissions_v2.search_timing import for_adapter
        started = time.perf_counter()
        timing = for_adapter()
        observe = None
        if timing is None:
            index = self._index()
        else:
            with timing.gate("runtime_setup"):
                index = self._index()
            observe = timing.observe
            observe("runtime_setup", time.perf_counter() - started)
        return MessageSearchRelease(protocol=self.protocol, resolver=index.resolver, reviews=index.reviews, index=index,
                                    clock=lambda: int(time.time()), embedder=fake_embedder, observe=observe)


class Doors:
    """One request through each door, and the released bytes it came back with."""

    def __init__(self, fixture, *, batch_size: int, one_share: bool, units: int):
        from topos.permissions_v2 import search_transport
        self.fixture, self.node, self.batch_size = fixture, fixture.node, batch_size
        self.served = Served(fixture.node)
        os.environ["TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED"] = "true"
        os.environ[search_transport.BATCH_FLAG] = "true"
        os.environ["TOPOS_CP_STAMP_PUBKEY"] = base64.b64encode(
            self.node.cp_key.public_key().public_bytes_raw()).decode()
        search_transport.get_runtime = lambda: self.served
        if not one_share:   # N askers stand for N shares: a lock of its own per request (module docstring)
            search_transport._grant_lock = lambda _grant_id: threading.Lock()
        self.transport = search_transport
        self.authority = self.current_authority()
        queries = fixture.queries
        if len(queries) < batch_size + 1:
            raise SystemExit("not enough distinct queries")
        #: What an asker sends, in turn: a question, or a batch of consecutive distinct queries.
        self.units = {"answers": [(query,) for query in queries[:units]],
                      "batch": [tuple(queries[(start + offset) % len(queries)] for offset in range(batch_size))
                                for start in range(0, min(len(queries), units))]}

    def current_authority(self):
        from tests.permissions_v2.message_search_harness import owner
        with self.node.ledger._transaction() as conn:
            self.node.protocol._sync_protection(conn)
        with owner():
            return self.node.ledger.authority_snapshot(self.fixture.grant_id, now=int(time.time()))

    def prepare(self, door: str, unit, request_id: str):
        """Everything a request needs that is not the node's work: for a batch, its signed frame."""
        if door == "answers":
            return unit[0]
        from topos.permissions_v2.signing import MAX_TTL_SECONDS, parse_envelope, request_digest, sign_envelope
        from topos.relay_stamp import canonical_signing_payload
        now = int(time.time())
        life = MAX_TTL_SECONDS   # the longest an envelope may live: a batch slower than this is refused, as on a node
        items = []
        for number, query in enumerate(unit):
            payload = {"query": query, "k": 10}
            body = {**self.authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key",
                    "request_id": f"{request_id}:{number}", "request_type": "permissions.v2.search",
                    "request_hash": request_digest("permissions.v2.search", payload), "issued_at": now,
                    "expires_at": now + life}
            items.append({"envelope": sign_envelope(parse_envelope(body, signed=False), self.node.cp_key).model_dump(),
                          "intent": payload})
        message = {"id": request_id, "type": self.transport.BATCH_MESSAGE_TYPE,
                   "payload": {"items": items, "respond_by": (now + life) * 1000}}
        stamp = {"v": 1, "cls": "third_party", "client_id": "client-2", "acting_user": "actor-1", "iat": now,
                 "exp": now + life}
        stamp["sig"] = base64.b64encode(self.node.cp_key.sign(canonical_signing_payload(
            stamp, msg_id=message["id"], msg_type=message["type"]))).decode()
        message["principal_stamp"] = stamp
        return message

    def ask(self, door: str, prepared) -> str:
        """One request; the SHA-256 of what it released. Raises Refused when the door refuses."""
        from tests.permissions_v2.message_search_harness import recipient
        from topos.permissions_v2.canonical import PolicyError, canonical_bytes
        if door == "answers":
            try:
                with recipient():
                    adapter = self.served.message_search()
                    _current, _policy, output, _decision = adapter.retrieve_for_answer(
                        grant_id=self.fixture.grant_id, question=prepared, admitted_authority=self.authority)
            except PolicyError as exc:
                raise Refused(exc.code) from None
            return hashlib.sha256(canonical_bytes(output.model_dump())).hexdigest()
        socket = Socket()
        asyncio.run(self.transport.dispatch_message_search_batch(socket, prepared))
        frame = json.loads(socket.sent[-1])
        if frame.get("status") != "ok":
            raise Refused("permission_denied")
        return hashlib.sha256(canonical_bytes([item["output"] for item in frame["payload"]["items"]])).hexdigest()


# -- what runs beside the askers ---------------------------------------------------------------------------------------

def ingest_writer(stop, period: float, canonical: Path, out: list) -> None:
    """One small commit to the canonical database per `period`, under the gate: the shape of ingestion's queue claim."""
    from topos.storage.db.write_gate import begin_immediate, with_db_write
    conn = sqlite3.connect(str(canonical), timeout=30, isolation_level=None)
    try:
        number = 0
        while not stop.wait(period):
            number += 1
            asked = time.perf_counter()
            with with_db_write():
                held = time.perf_counter()
                begin_immediate(conn)
                conn.execute("INSERT INTO nr166_ingest(n) VALUES (?)", (number,))
                conn.commit()
            out.append((held - asked, time.perf_counter() - held))
    finally:
        conn.close()


def reviews_writer(stop, period: float, fixture, out: list) -> None:
    """One machine assessment of a message outside the share per `period`, as an assessment catch-up publishes them."""
    from tests.permissions_v2 import direct_search_twins as dst
    from tests.permissions_v2.message_search_harness import owner
    from topos.permissions_v2.automatic_message_review import parse_assessment, prepare, publish
    resolver, reviews = fixture.node.corpus.resolver, fixture.node.corpus.reviews
    while fixture.spare and not stop.wait(period):
        native = fixture.spare.pop()
        started = time.perf_counter()
        with owner():
            identity = resolver._identity("conversation_messages", native.message_id, "imessage", dst.DATASET)
            prepared = prepare(resolver, reviews, identity)
            publish(resolver, reviews, prepared, parse_assessment(dict(domains=["hobbies"], sensitivity="none",
                speech="original_message", protected_content="none"), prepared["snapshot"].message),
                now=int(time.time()))
        out.append(time.perf_counter() - started)


def sweeper(stop, interval: float, index, out: list) -> None:
    """The runtime's sweeper loop (`runtime._start_sweeper`); the first sweep a second into the cell."""
    from topos.permissions_v2.search_timing import timed_sweep
    delay = min(1.0, interval)
    while not stop.wait(delay):
        delay = interval
        started = time.perf_counter()
        removed = timed_sweep(index)
        out.append((time.perf_counter() - started, removed))


def read_probe(stop, interval: float, canonical: Path, out: list) -> None:
    """The owner's plain read: one point read on its own read-only connection, no gate."""
    conn = sqlite3.connect(canonical.as_uri() + "?mode=ro", uri=True)
    try:
        while not stop.wait(interval):
            started = time.perf_counter()
            conn.execute("SELECT value FROM engine_config WHERE key='user_id'").fetchone()
            out.append(time.perf_counter() - started)
    finally:
        conn.close()


def write_probe(stop, interval: float, scratch: Path, out: list) -> None:
    """The owner's small write: the gate, then one row in a scratch database (so it moves no token of the share's)."""
    from topos.storage.db.write_gate import with_db_write
    conn = sqlite3.connect(str(scratch), timeout=30)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS probe(n INTEGER)")
        conn.commit()
        number = 0
        while not stop.wait(interval):
            number += 1
            started = time.perf_counter()
            with with_db_write():
                conn.execute("INSERT INTO probe(n) VALUES (?)", (number,))
                conn.commit()
            out.append(time.perf_counter() - started)
    finally:
        conn.close()


# -- one cell ------------------------------------------------------------------------------------------------------------

def parse_lines(texts):
    """The kept timing lines as `search_timing_attribution.timing_lines` yields them from a log file."""
    import search_timing_attribution as analyzer
    for text in texts:
        match = analyzer.LINE.search(text)
        if match:
            yield match.group(1), match.group(2), float(match.group(3)), dict(
                pair.split("=", 1) for pair in match.group(4).split()), None


def stage_table(door: str, parsed) -> dict:
    """Per request (run) of this door: each stage's time and the named parts; then the median over requests."""
    runs: dict = {}
    for run, stage, ms, fields, _wall in parsed:
        if stage not in STAGES[door]:
            continue
        entry = runs.setdefault(run, {"stages": {}, "parts": {}})
        entry["stages"][stage] = entry["stages"].get(stage, 0.0) + ms
        for part in PARTS:
            if f"{part}_ms" in fields:
                entry["parts"][f"{stage}.{part}"] = float(fields[f"{part}_ms"])
    marker = "answer_walk" if door == "answers" else "transport_total"
    done = [entry for entry in runs.values() if marker in entry["stages"]]

    def median(kind, key):
        values = [entry[kind][key] for entry in done if key in entry[kind]]
        return round(statistics.median(values), 3) if values else None
    return {"requests": len(done),
            "stages_ms_p50": {stage: median("stages", stage) for stage in STAGES[door]},
            "parts_ms_p50": {key: median("parts", key) for key in sorted({k for e in done for k in e["parts"]})}}


def run_cell(fixture, doors: Doors, lines: Lines, slow: SlowSections, *, door: str, askers: int, per_asker: int,
             kind: str, reference: dict, options) -> dict:
    import search_timing_attribution as analyzer
    from topos.permissions_v2 import search_timing
    os.environ[TIMINGS], os.environ[ATTRIBUTION] = "true", ("false" if options.no_attribution else "true")
    os.environ[OUTSIDE] = "true" if options.decide_outside_gate else "false"
    search_timing.attributing()   # installs (or removes) the gate's observer before the first section
    search_timing.totals(reset=True)
    lines.take()
    slow.count = 0
    doors.authority = doors.current_authority()
    stop = threading.Event()
    writes, assessed, sweeps, reads, owner_writes = [], [], [], [], []
    beside = [threading.Thread(target=read_probe, name="nr166-read-probe", daemon=True,
                               args=(stop, options.probe_interval, fixture.canonical, reads)),
              threading.Thread(target=write_probe, name="nr166-write-probe", daemon=True,
                               args=(stop, options.write_probe_interval, fixture.root / "probe.db", owner_writes))]
    if kind != "idle":
        if options.writer_period > 0:
            beside.append(threading.Thread(target=ingest_writer, name="nr166-ingest-writer", daemon=True,
                                           args=(stop, options.writer_period, fixture.canonical, writes)))
        if options.sweep_interval > 0:
            beside.append(threading.Thread(target=sweeper, name="p2c-index-sweep", daemon=True,
                                           args=(stop, options.sweep_interval, fixture.node.index, sweeps)))
    if kind == "reviews":
        beside.append(threading.Thread(target=reviews_writer, name="nr166-reviews-writer", daemon=True,
                                       args=(stop, options.reviews_period, fixture, assessed)))
    units = doors.units[door]
    results, errors = [], []
    barrier = threading.Barrier(askers)
    deadline = time.monotonic() + options.cell_seconds

    def asker(number: int) -> None:
        try:
            barrier.wait()
            for turn in range(per_asker):
                if time.monotonic() >= deadline:
                    break
                index = (number * 7 + turn) % len(units)
                prepared = doors.prepare(door, units[index], f"nr166-{fixture.members}-{kind}-{door}-{askers}-{number}-{turn}")
                started = time.perf_counter()
                try:
                    released = doors.ask(door, prepared)
                except Refused:
                    released = None
                results.append((time.perf_counter() - started, index, released))
        except Exception as exc:  # noqa: BLE001 -- the class only: a failed asker must not hide in a thread
            errors.append(type(exc).__name__)

    load_before = os.getloadavg()
    started = time.perf_counter()
    threads = [threading.Thread(target=asker, name=f"nr166-asker-{number}", args=(number,)) for number in range(askers)]
    for thread in beside + threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started
    stop.set()
    for thread in beside:
        thread.join(timeout=120)
    stragglers = [thread.name for thread in beside if thread.is_alive()]
    search_timing.emit_totals(every=0)
    parsed = list(parse_lines(lines.take()))
    attribution = analyzer.load_attribution(parsed)
    os.environ[TIMINGS] = os.environ[ATTRIBUTION] = os.environ[OUTSIDE] = "false"
    search_timing.attributing()

    latencies = [seconds * 1000 for seconds, _index, released in results if released is not None]
    budget = options.answers_budget if door == "answers" else options.batch_budget
    own = [entry for entry in attribution["requests"].values()
           if (entry["door"] == "answers") == (door == "answers") and entry["gate_hold_ms"] > 0]
    return {
        "members": fixture.members, "door": door, "askers": askers, "kind": kind, "per_asker": per_asker,
        "seconds": round(elapsed, 3), "requests": len(results), "truncated": len(results) < askers * per_asker,
        "refused": sum(1 for _s, _i, released in results if released is None), "asker_errors": errors,
        "stragglers": stragglers, "spare_left": len(fixture.spare),
        "mismatches": sum(1 for _s, index, released in results
                          if released is not None and released != reference[door][index]),
        "over_budget": sum(1 for value in latencies if value > budget * 1000),
        "latency_ms": spread(latencies),
        "refused_ms": spread([seconds * 1000 for seconds, _index, released in results if released is None]),
        "throughput_per_s": round(len(latencies) / elapsed, 3) if elapsed else None,
        "request_gate_hold_ms": spread([entry["gate_hold_ms"] for entry in own]),
        "request_gate_wait_ms": spread([entry["gate_wait_ms"] for entry in own]),
        "stages": stage_table(door, parsed),
        "sites": attribution["sites"], "fast_paths": attribution["fast_paths"],
        "attribution_dropped": attribution["dropped"],
        "probe_read_ms": spread([value * 1000 for value in reads]),
        "probe_write_ms": spread([value * 1000 for value in owner_writes]),
        "writer": {"commits": len(writes), "wait_ms": spread([wait * 1000 for wait, _hold in writes]),
                   "hold_ms": spread([hold * 1000 for _wait, hold in writes])},
        "reviews_writer": {"published": len(assessed), "ms": spread([value * 1000 for value in assessed])},
        "sweeps": {"n": len(sweeps), "ms": spread([seconds * 1000 for seconds, _removed in sweeps]),
                   "removed": sum(removed or 0 for _seconds, removed in sweeps if isinstance(removed, int))},
        "slow_sections": slow.count, "load_avg": {"before": load_before, "after": os.getloadavg()},
    }


def reference_pass(doors: Doors) -> dict:
    """Every unit once through its door with timing and attribution off, decided under the gate: the bytes every
    later request must match, whatever is switched on for the cells."""
    os.environ[TIMINGS] = os.environ[ATTRIBUTION] = os.environ[OUTSIDE] = "false"
    from topos.permissions_v2 import search_timing
    search_timing.attributing()
    found = {}
    for door in DOORS:
        found[door] = [doors.ask(door, doors.prepare(door, unit, f"nr166-reference-{door}-{number}"))
                       for number, unit in enumerate(doors.units[door])]
    return found


# -- the table -----------------------------------------------------------------------------------------------------------

def _ms(value) -> str:
    return "-" if value is None else f"{value:,.0f}"


def _rate(cell, path: str) -> str:
    entry = cell["fast_paths"].get(path)
    if not entry or entry["hit_rate"] is None:
        return "-"
    return f"{entry['hit_rate']:.0%} ({entry['hit']}/{entry['hit'] + entry['miss']})"


def table(report: dict) -> str:
    out = ["# NR166 R0 load table", "",
           f"Engine `{report['engine']}`{' (dirty)' if report['engine_dirty'] else ''}, attribution "
           f"{'off' if report['options']['no_attribution'] else 'on'}, decided "
           f"{'OUTSIDE the gate (R1 switch on)' if report['options'].get('decide_outside_gate') else 'under the gate'}, "
           f"writer every {report['options']['writer_period']} s, "
           f"sweep every {report['options']['sweep_interval']} s, batches of {report['options']['batch_size']}, "
           f"universe x{report['options']['universe']}. "
           f"Load average at start {report['load_avg_start'][0]:.1f}, at end {report['load_avg_end'][0]:.1f}. Times in ms.",
           "",
           "| members | door | cell | askers | n | p50 | p95 | max | over budget | own gate hold p50 | own gate wait p95 "
           "| owner read p95 | owner write p95 | writer wait p95 | N3a boundary | N3a digest | N5 keep | N5 send "
           "| BL-171 guard | R1 confirm | slow sections | refused | mismatches | load |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for cell in report["cells"]:
        lat = cell["latency_ms"]
        out.append("| " + " | ".join(str(value) for value in (
            cell["members"], cell["door"], cell["kind"], cell["askers"], lat["n"], _ms(lat["p50"]), _ms(lat["p95"]),
            _ms(lat["max"]), cell["over_budget"], _ms(cell["request_gate_hold_ms"]["p50"]),
            _ms(cell["request_gate_wait_ms"]["p95"]), _ms(cell["probe_read_ms"]["p95"]),
            _ms(cell["probe_write_ms"]["p95"]), _ms(cell["writer"]["wait_ms"]["p95"]), _rate(cell, "n3a_boundary"),
            _rate(cell, "n3a_digest"), _rate(cell, "n5_keep"), _rate(cell, "n5_send"), _rate(cell, "bl171_guard"),
            _rate(cell, "r1_confirm"), cell["slow_sections"], cell["refused"], cell["mismatches"], f"{cell['load_avg']['after'][0]:.1f}")) + " |")
    out += ["", "## Gate sections by site and caller", "",
            "Whole process, per cell: the eight sites with the most hold plus wait. `own` sections are the thread's "
            "outermost; `nested` ones lie inside an own section of the same thread.", ""]
    for cell in report["cells"]:
        out += [f"**{cell['members']} members, {cell['door']}, {cell['kind']}, {cell['askers']} askers**", "",
                "| | site | caller | n | hold total | hold longest | wait total | wait longest |", "|---|---|---|---|---|---|---|---|"]
        ranked = sorted(cell["sites"], key=lambda site: -(site["hold_ms"] + site["wait_ms"]))[:8]
        for site in ranked:
            out.append(f"| {'nested' if site['nested'] else 'own'} | `{site['site']}` | `{site['caller']}` | {site['n']} | "
                       f"{_ms(site['hold_ms'])} | {_ms(site['longest_ms'])} | {_ms(site['wait_ms'])} | "
                       f"{_ms(site['longest_wait_ms'])} |")
        out.append("")
    out += ["## Stage medians per request", ""]
    for cell in report["cells"]:
        stages = {stage: value for stage, value in cell["stages"]["stages_ms_p50"].items() if value is not None}
        parts = {part: value for part, value in cell["stages"]["parts_ms_p50"].items() if value is not None}
        out += [f"- **{cell['members']} members, {cell['door']}, {cell['kind']}, {cell['askers']} askers**: "
                + ", ".join(f"{stage} {value:,.0f}" for stage, value in stages.items())
                + ("; parts: " + ", ".join(f"{part} {value:,.0f}" for part, value in parts.items()) if parts else "")]
    out += ["", "## Fast paths, misses by what moved", ""]
    for cell in report["cells"]:
        moved = {path: entry["moved"] for path, entry in cell["fast_paths"].items() if entry["moved"]}
        firsts = {path: (entry["first"], entry["skip"]) for path, entry in cell["fast_paths"].items()}
        out.append(f"- **{cell['members']} members, {cell['door']}, {cell['kind']}, {cell['askers']} askers**: "
                   f"{json.dumps(moved, sort_keys=True)}; first/skip {json.dumps(firsts, sort_keys=True)}")
    return "\n".join(out) + "\n"


def compare(first: Path, second: Path) -> int:
    """Two runs match when every cell's p50 is within MATCH_RATIO or MATCH_FLOOR_MS of the other's."""
    one, two = json.loads(first.read_text()), json.loads(second.read_text())

    def keyed(report):
        return {(cell["members"], cell["door"], cell["kind"], cell["askers"]): cell for cell in report["cells"]}
    a, b = keyed(one), keyed(two)
    differing = 0
    print("| members | door | cell | askers | p50 run 1 | p50 run 2 | ratio | p95 run 1 | p95 run 2 | match |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for key in sorted(set(a) | set(b), key=str):
        if key not in a or key not in b:
            differing += 1
            print("| " + " | ".join(str(part) for part in key) + " | - | - | - | - | - | missing |")
            continue
        x, y = a[key]["latency_ms"], b[key]["latency_ms"]
        if x["p50"] is None or y["p50"] is None:
            differing += 1
            print("| " + " | ".join(str(part) for part in key) + " | - | - | - | - | - | no samples |")
            continue
        low, high = sorted((x["p50"], y["p50"]))
        match = high <= low * MATCH_RATIO or high - low <= MATCH_FLOOR_MS
        differing += not match
        print("| " + " | ".join(str(part) for part in key) + f" | {_ms(x['p50'])} | {_ms(y['p50'])} | "
              f"{(y['p50'] / x['p50']) if x['p50'] else 0:.2f} | {_ms(x['p95'])} | {_ms(y['p95'])} | "
              f"{'yes' if match else 'NO'} |")
    print(f"\n{differing} of {len(set(a) | set(b))} cells differ (rule: p50 within x{MATCH_RATIO} or {MATCH_FLOOR_MS:.0f} ms)")
    return 1 if differing else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--compare", nargs=2, type=Path, metavar="REPORT", help="compare two reports and exit")
    parser.add_argument("--out", type=Path, help="directory for report.json and table.md")
    parser.add_argument("--members", default="100,300,600")
    parser.add_argument("--askers", default="1,3,5,10")
    parser.add_argument("--doors", default="answers,batch")
    parser.add_argument("--cells", default="idle,grid,reviews", help="which kinds of cell to run")
    parser.add_argument("--requests", type=int, default=16, help="requests per cell to aim for")
    parser.add_argument("--min-per-asker", type=int, default=3)
    parser.add_argument("--cell-seconds", type=float, default=240.0, help="no new request starts after this long")
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--units", type=int, default=12, help="distinct questions, and distinct batches, asked in turn")
    parser.add_argument("--spare", type=int, default=80, help="unassessed messages kept for the reviews writer")
    parser.add_argument("--universe", type=int, default=10, help="identity universe units no member touches (0: none)")
    parser.add_argument("--writer-period", type=float, default=0.25)
    parser.add_argument("--reviews-period", type=float, default=1.0)
    parser.add_argument("--sweep-interval", type=float, default=10.0)
    parser.add_argument("--probe-interval", type=float, default=0.1)
    parser.add_argument("--write-probe-interval", type=float, default=0.5)
    parser.add_argument("--batch-budget", type=float, default=20.0, help="seconds: the relay's wait for a batch")
    parser.add_argument("--answers-budget", type=float, default=15.0, help="seconds: the answers door's dispatch wait")
    parser.add_argument("--one-share", action="store_true", help="keep the transport's one-batch-per-share lock")
    parser.add_argument("--no-attribution", action="store_true", help="timing lines only (to price attribution)")
    parser.add_argument("--decide-outside-gate", action="store_true",
                        help="NR166 R1: the cells run with DECIDE_OUTSIDE_GATE on (the reference pass never does)")
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--scratch", type=Path)
    args = parser.parse_args(argv)
    if args.compare:
        return compare(*args.compare)
    if args.out is None:
        parser.error("--out is required")

    import topos
    from topos.permissions_v2 import search_timing
    print(f"topos from {Path(topos.__file__).resolve().parent}", flush=True)
    lines, slow = Lines(), SlowSections()
    timing_log = logging.getLogger("topos.permissions_v2.search_timing")
    timing_log.setLevel(logging.INFO)
    timing_log.addHandler(lines)
    timing_log.propagate = False
    logging.getLogger("topos.storage.db.write_gate").addHandler(slow)
    os.environ[TIMINGS] = os.environ[ATTRIBUTION] = "false"

    engine = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "topos", "scripts"], capture_output=True,
                                text=True, cwd=ROOT).stdout.strip())
    kinds = [kind for kind in args.cells.split(",") if kind]
    report = {"schema": "NR166-R0-load/v1", "engine": engine, "engine_dirty": dirty,
              "options": {key: (str(value) if isinstance(value, Path) else value) for key, value in vars(args).items()
                          if key not in ("compare", "out", "scratch")},
              "load_avg_start": os.getloadavg(), "started_at": int(time.time()), "fixtures": [], "cells": []}
    scratch = args.scratch or Path(tempfile.mkdtemp(prefix="nr166-load-"))
    scratch = scratch.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    for members in (int(value) for value in args.members.split(",")):
        started = time.perf_counter()
        fixture = build_fixture(scratch / f"fixture-{members}-{int(time.time())}", members=members, spare=args.spare,
                                seed=args.seed, universe=args.universe)
        doors = Doors(fixture, batch_size=args.batch_size, one_share=args.one_share, units=args.units)
        build_s = time.perf_counter() - started
        if "reviews" in kinds or "grid" in kinds:
            # One assessment outside the share before anything is asked, as on a node whose catch-up has run since
            # the build: the review digest is no longer the index's, so every recheck consults the review guard.
            first, done = threading.Event(), []
            worker = threading.Thread(target=reviews_writer, args=(first, 0.01, SimpleNamespace(
                node=fixture.node, spare=[fixture.spare.pop()]), done))
            worker.start()
            worker.join()
            if len(done) != 1:
                raise SystemExit("the first assessment outside the share was not published")
        started = time.perf_counter()
        reference = reference_pass(doors)
        report["fixtures"].append({"members": members, "build_s": round(build_s, 1), "universe": fixture.universe,
                                   "reference_s": round(time.perf_counter() - started, 1),
                                   "load_avg": os.getloadavg()})
        print(f"fixture {members}: built in {build_s:.0f} s, reference pass {time.perf_counter() - started:.1f} s",
              flush=True)
        plan = []
        for door in (door for door in args.doors.split(",") if door):
            if "idle" in kinds:
                plan.append((door, 1, "idle"))
            if "grid" in kinds:
                plan += [(door, int(value), "grid") for value in args.askers.split(",")]
            if "reviews" in kinds:
                plan.append((door, 1, "reviews"))
        for door, askers, kind in plan:
            per_asker = max(args.min_per_asker, math.ceil(args.requests / askers))
            cell = run_cell(fixture, doors, lines, slow, door=door, askers=askers, per_asker=per_asker, kind=kind,
                            reference=reference, options=args)
            report["cells"].append(cell)
            lat = cell["latency_ms"]
            print(f"  {members:>4} {door:7} {kind:7} askers={askers:<2} n={lat['n']:<3} p50={_ms(lat['p50']):>7} "
                  f"p95={_ms(lat['p95']):>7} hold={_ms(cell['request_gate_hold_ms']['p50']):>6} "
                  f"wait95={_ms(cell['request_gate_wait_ms']['p95']):>7} refused={cell['refused']} "
                  f"mismatch={cell['mismatches']} load={cell['load_avg']['after'][0]:.1f} ({cell['seconds']:.0f} s)",
                  flush=True)
    report["load_avg_end"] = os.getloadavg()
    report["ended_at"] = int(time.time())
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True, default=str) + "\n")
    text = table(report)
    (args.out / "table.md").write_text(text)
    print(text.split("## Gate sections")[0])
    bad = sum(cell["mismatches"] + len(cell["asker_errors"]) for cell in report["cells"])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
