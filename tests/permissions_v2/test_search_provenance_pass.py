"""WS4 N3c: each search pass proves its recovered iMessage dependencies with ONE provenance service, and runs the
store check (`_check`) and the snapshot re-hash ONCE, after the pass's last member.

A search validates its grant's index three times (index load, the gated recheck, the send check). Before N3c
every recovered iMessage dependency built a new IngestProvenanceService (and EvidenceResolver) and ran a
gate-held `_check` twice and a snapshot re-hash, in every pass. Pinned here:
- a pass builds one service and checks once, after its last member; nothing is shared across passes or searches;
- what a search releases is byte-identical to proving every dependency on its own;
- the revocation caveat (WS3): a revocation committed between two members of the send-time pass refuses, because
  the one `_check` runs after the last member -- a `_check` at the start of the pass would miss it;
- a revoked enrollment, a moved source clock, a ledger rollback or a replaced snapshot fails the pass, whether it
  landed before the pass's snapshot (the member's own reads, or the end check, see it) or between two members
  (the end check sees it); every proof the old path refuses, the pass refuses;
- a pass validates nothing after it is finished or closed, on another connection, or outside a read transaction;
- the write gate is released on every path;
- IF-3 v1.4: the members split (dependency loads, their boundary checks, the pass's setup, check and re-hash) and
  the pass's two gate waits, content-free.

Only an active Off-limits boundary gives a direct member dependencies to load (`direct_search_twins` alone has
none), so the fixture adds one, on a person no member mentions. Since N5 a quiet search runs the member loop once
(the gated recheck: index load checks the basis only, and the send check skips when nothing moved), so tests of the
send-time pass run the send check in full (`full_send_check`), as it runs whenever anything moved. Commits that must land inside an open read need
WAL; under the rollback journal the writer waits for the reader.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

import pytest

from tests.permissions_v2 import direct_search_twins as dst
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_imessage_reconciliation import snapshot as native_snapshot
from tests.permissions_v2.test_message_search_refusals import Socket, relay_message, signed
from tests.permissions_v2.test_search_timing_attribution import FLAG, LOGGER, by_stage, hold_gate, parsed
from topos.permissions_v2 import reconciliation_provenance, search_timing, search_transport
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceResolver
from topos.permissions_v2.ingest_provenance import IngestProvenanceService
from topos.permissions_v2.reconciliation_provenance import ExistingProvenancePass, validate_existing
from topos.permissions_v2.search_index import SearchIndexService, SearchVerification
from topos.storage.db import write_gate

MEMBERS = 6


@pytest.fixture
def node(tmp_path):
    built = dst.build(tmp_path / "direct", members=MEMBERS, hidden_facts=0, seed=9, protected=True)
    built.query = {"query": dst.queries(MEMBERS, 9, 1)[0], "k": 5}
    return built


def wal(node):
    with sqlite3.connect(node.corpus.path) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"


def service_for(node):
    path = node.corpus.path
    return IngestProvenanceService(canonical_database=path, binding=dst.BINDING,
                                   snapshot_root=path.parent / "permissions-v2" / "ingest-snapshots")


def enrollment_id(node):
    with sqlite3.connect(node.corpus.path) as conn:
        [(value,)] = conn.execute("SELECT enrollment_id FROM ingest_provenance_enrollments").fetchall()
    return value


def snapshot_path(node):
    return node.corpus.path.parent / "permissions-v2" / "ingest-snapshots" / "canary.db"


def _write(node, action):
    conn = sqlite3.connect(node.corpus.path, timeout=0.5)
    try:
        with owner():
            action(conn)
        conn.commit()
    finally:
        conn.close()


# The four changes that move a provenance outcome while the member's content and the protection clock may not.

def revoke(node):
    """The owner revokes the native enrollment (the store's own revocation: marker first, then the commit)."""
    _write(node, lambda conn: service_for(node).revoke(conn, enrollment_id=enrollment_id(node), source_id="imessage"))


def disable_source(node, *, observed=True):
    """The iMessage source is switched off: its trigger moves the source clock. `observed`: a node reader's `_check`
    then records that generation in the marker, as the node's next ingest or provenance read does."""
    _write(node, lambda conn: conn.execute("UPDATE user_ingestion_sources SET enabled=0 WHERE dataset_id=?", (dst.DATASET,)))
    if observed:
        with sqlite3.connect(node.corpus.path) as conn:
            service_for(node)._check(conn)


def ledger_write(node):
    """A legitimate ledger transaction (an owner command burned): the marker moves past any older snapshot."""
    _write(node, lambda conn: service_for(node).consume_command(conn, command_id="n3c-command", command_hash="a" * 64))


def roll_back_ledger(node):
    """The ledger rolled back under the marker: the command just burned is gone from the canonical rows only."""
    ledger_write(node)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("DELETE FROM ingest_provenance_commands WHERE command_id='n3c-command'")


def replace_snapshot(node):
    """The native snapshot file its enrollment pins is replaced by different bytes (a valid, closed snapshot)."""
    path = snapshot_path(node)
    data = native_snapshot(count=MEMBERS, mutate=lambda db: db.execute("UPDATE message SET text='replaced'"))
    fresh = path.with_name("replacement.db")
    fresh.write_bytes(data)
    fresh.chmod(0o400)
    os.replace(fresh, path)


BEFORE = {"revoked_enrollment": revoke, "source_clock": lambda node: disable_source(node, observed=False),
          "ledger_rollback": roll_back_ledger, "replaced_snapshot": replace_snapshot}
BETWEEN = {"revoked_enrollment": revoke, "source_clock": disable_source, "ledger_write": ledger_write,
           "replaced_snapshot": replace_snapshot}


def read_snapshot(node):
    """A read transaction on the canonical database, its snapshot established, as a pass reads."""
    conn = sqlite3.connect(f"file:{node.corpus.path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    conn.execute("SELECT generation FROM ingest_provenance_state").fetchall()
    return conn


def a_pass(node, conn, **kwargs):
    return ExistingProvenancePass(conn, canonical_database=node.corpus.path, binding=dst.BINDING, **kwargs)


def prove(provenance, conn, unit):
    return provenance.validate(conn, message_id=unit.message_id, dataset_id=dst.DATASET, with_classification=True)


def recorded_passes(monkeypatch) -> list:
    made = []
    original = ExistingProvenancePass.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.events, self.services = [], set()
        made.append(self)
    monkeypatch.setattr(ExistingProvenancePass, "__init__", init)
    validate, finish = ExistingProvenancePass.validate, ExistingProvenancePass.finish

    def recorded_validate(self, *args, **kwargs):
        self.events.append("validate")
        try:
            return validate(self, *args, **kwargs)
        finally:
            if self._service is not None:
                self.services.add(id(self._service))

    def recorded_finish(self):
        try:
            finish(self)
        except PolicyError:
            self.events.append("refused")
            raise
        self.events.append("finished")
    monkeypatch.setattr(ExistingProvenancePass, "validate", recorded_validate)
    monkeypatch.setattr(ExistingProvenancePass, "finish", recorded_finish)
    return made


def recorded_checks(monkeypatch) -> list:
    """Every `_check` any IngestProvenanceService runs: (service, outcome code or 'ok')."""
    checks = []
    original = IngestProvenanceService._check

    def check(self, conn):
        try:
            generation = original(self, conn)
        except PolicyError as exc:
            checks.append((self, exc.code))
            raise
        checks.append((self, "ok"))
        return generation
    monkeypatch.setattr(IngestProvenanceService, "_check", check)
    return checks


async def relayed(node, monkeypatch, request_id):
    message = relay_message(node, signed(node, payload=node.query, request_id=request_id), node.query, monkeypatch,
                            request_id=request_id)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    [frame] = [json.loads(value) for value in socket.sent]
    return frame


def full_send_check(monkeypatch):
    """The send check's member loop runs, as it does whenever anything moved since the recheck (N5's token)."""
    monkeypatch.setattr(SearchIndexService, "send_token", lambda self, *args, **kwargs: None)


def own_check(node, verified, **kwargs):
    grant_id = node.search_raw["binding"]["grant_id"]
    with node.ledger._transaction() as db:
        node.protocol._sync_protection(db)
        authority = node.ledger._authority(db, grant_id, node.now[0])[0]
    return node.index.check_own(grant_id, authority, now=node.now[0], verified=verified, **kwargs)


def after_first_member(monkeypatch, change, *, armed=lambda: True):
    """Run `change` once, after the first member's dependency load of the first armed pass, before the second's."""
    fired = []
    original = SearchIndexService._entity_dependencies_current

    def dependencies(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if armed() and not fired:
            fired.append(True)
            change()
        return result
    monkeypatch.setattr(SearchIndexService, "_entity_dependencies_current", dependencies)
    return fired


# -- one service and one check per pass, after its last member ---------------------------------------------------

@pytest.mark.asyncio
async def test_each_pass_builds_one_service_and_checks_once_after_its_last_member(node, monkeypatch):
    made, checks = recorded_passes(monkeypatch), recorded_checks(monkeypatch)
    full_send_check(monkeypatch)
    frame = await relayed(node, monkeypatch, "n3c-quiet")
    assert frame["status"] == "ok" and frame["payload"]["output"]["records"]
    assert len(made) == 2  # the gated recheck and the send check (index load checks the basis only, N5)
    for provenance in made:
        # Every member proven through the pass (not vacuous), then its one check, and nothing after it.
        assert provenance.events.count("validate") >= MEMBERS - 1
        assert provenance.events[-1] == "finished" and provenance.events.count("finished") == 1
        assert provenance._closed and provenance._service is not None
        assert provenance.services == {id(provenance._service)}  # built once, at the first dependency
        assert [code for service, code in checks if service is provenance._service] == ["ok"]


@pytest.mark.asyncio
async def test_nothing_is_shared_across_passes_or_searches(node, monkeypatch):
    made = recorded_passes(monkeypatch)
    full_send_check(monkeypatch)
    for number in range(2):
        frame = await relayed(node, monkeypatch, f"n3c-share-{number}")
        assert frame["status"] == "ok" and frame["payload"]["output"]["records"]
    assert len(made) == 4
    services = [provenance._service for provenance in made]
    resolvers = [service.resolver for service in services]
    assert len({id(value) for value in made}) == len({id(value) for value in services}) == 4
    assert len({id(value) for value in resolvers}) == 4
    assert all(isinstance(resolver, EvidenceResolver) and resolver is not node.corpus.resolver for resolver in resolvers)
    for first in range(0, 4, 2):  # within a search, each pass reads its own stage's snapshot
        assert len({id(provenance.conn) for provenance in made[first:first + 2]}) == 2
    for provenance in made:  # a finished pass proves nothing more, on any connection
        assert provenance._closed
        with pytest.raises(PolicyError):
            provenance.validate(provenance.conn, message_id=node.corpus.units[0].message_id, dataset_id=dst.DATASET)
        with pytest.raises(PolicyError):
            provenance.finish()
    assert not [value for value in vars(SearchVerification).values() if isinstance(value, ExistingProvenancePass)]


@pytest.mark.asyncio
async def test_the_pass_releases_exactly_what_proving_each_dependency_releases(node, monkeypatch):
    once = await relayed(node, monkeypatch, "n3c-same-1")
    original = EvidenceResolver._existing_native_origin
    monkeypatch.setattr(EvidenceResolver, "_existing_native_origin",
                        lambda self, conn, identity, provenance=None: original(self, conn, identity))
    made = recorded_passes(monkeypatch)
    each = await relayed(node, monkeypatch, "n3c-same-2")
    assert made and not any("validate" in provenance.events for provenance in made)  # the per-dependency path ran
    assert once["status"] == each["status"] == "ok" and once["payload"]["output"]["records"]
    assert json.dumps(once["payload"]["output"], sort_keys=True) == json.dumps(each["payload"]["output"], sort_keys=True)


# -- the revocation caveat: the one check runs after the last member ----------------------------------------------

@pytest.mark.asyncio
async def test_a_revocation_between_two_members_of_the_send_time_pass_refuses(node, monkeypatch):
    """WS3's caveat, and the brief's test. The send check's pass reads one snapshot, established before the owner's
    revocation commits between its first and second member; only the marker, read by the pass's one `_check` after
    its LAST member, can see it. A `_check` at the start of the pass passes, every member passes on the snapshot,
    and the search would be sent: the mutant `check_at_the_start_of_the_pass` must fail here."""
    wal(node)
    made, checks = recorded_passes(monkeypatch), recorded_checks(monkeypatch)
    full_send_check(monkeypatch)
    dispatched = []
    original = node.search.dispatch

    def dispatch(**kwargs):
        result = original(**kwargs)
        dispatched.append(True)  # checkpointed: the next pass is the send check's
        return result
    monkeypatch.setattr(node.search, "dispatch", dispatch)
    fired = after_first_member(monkeypatch, lambda: revoke(node), armed=lambda: bool(dispatched))
    frame = await relayed(node, monkeypatch, "n3c-revoke")
    assert fired == [True] and frame["status"] == "error"
    with sqlite3.connect(node.corpus.path) as conn:  # the revocation really committed
        assert conn.execute("SELECT state FROM ingest_provenance_enrollments").fetchone()[0] == "revoked"
    recheck, send_check = made
    assert recheck.events[-1] == "finished"
    # Every member of the send-time pass passed on its snapshot; the pass's one check, after them, refused.
    assert send_check.events.count("validate") >= MEMBERS - 1 and send_check.events[-1] == "refused"
    assert [code for service, code in checks if service is send_check._service] == ["ingest_ledger_rollback"]


@pytest.mark.parametrize("change", sorted(BETWEEN))
def test_a_change_between_two_members_refuses_the_pass(node, monkeypatch, caplog, change):
    wal(node)
    made = recorded_passes(monkeypatch)
    fired = after_first_member(monkeypatch, lambda: BETWEEN[change](node))
    with node.search.verification() as verified, pytest.raises(PolicyError) as refused:
        own_check(node, verified)
    assert fired == [True] and refused.value.code == "search_index_stale"
    [provenance] = made
    assert provenance.events.count("validate") >= MEMBERS - 1 and provenance.events[-1] == "refused"
    assert any(record.getMessage() == "message search index stale (member_unavailable)" for record in caplog.records)


@pytest.mark.parametrize("change", sorted(BETWEEN))
def test_a_change_between_two_members_reaches_only_the_end_of_the_pass(node, change):
    """Pass level: members after the change still pass on the snapshot (the change is invisible to it), and `finish`,
    after the last of them, refuses. The path before N3c refused at the next member's own `_check` or re-hash."""
    wal(node)
    units = node.corpus.units
    conn = read_snapshot(node)
    try:
        provenance = a_pass(node, conn)
        prove(provenance, conn, units[0])
        BETWEEN[change](node)
        for unit in units[1:]:
            prove(provenance, conn, unit)
        with pytest.raises(PolicyError):
            provenance.finish()
    finally:
        conn.close()


@pytest.mark.parametrize("change", sorted(BEFORE))
def test_a_change_before_the_pass_fails_the_pass_as_it_fails_each_proof(node, change):
    """In the snapshot: the member's own reads (enrollment state, source generation) or the end check (ledger,
    snapshot file) refuse, and so does the path before N3c for every member."""
    BEFORE[change](node)
    units = node.corpus.units
    conn = read_snapshot(node)
    try:
        for unit in units:
            with pytest.raises(PolicyError):
                validate_existing(service_for(node), conn, message_id=unit.message_id, dataset_id=dst.DATASET)
        provenance = a_pass(node, conn)
        with pytest.raises(PolicyError):
            for unit in units:
                prove(provenance, conn, unit)
            provenance.finish()
    finally:
        conn.close()


def test_a_quiet_pass_matches_each_proof_byte_for_byte(node):
    units = node.corpus.units
    conn = read_snapshot(node)
    try:
        each = [validate_existing(service_for(node), conn, message_id=unit.message_id, dataset_id=dst.DATASET,
                                  with_classification=True) for unit in units]
        provenance = a_pass(node, conn)
        once = [prove(provenance, conn, unit) for unit in units]
        provenance.finish()
    finally:
        conn.close()
    assert len(once) == MEMBERS and all(item["_p2b_native_event_nanoseconds"] for item in once)
    assert once == each


# -- what a pass refuses to do ------------------------------------------------------------------------------------

def test_a_pass_proves_only_on_its_own_open_read_and_only_until_it_ends(node):
    unit = node.corpus.units[0]
    conn, other = read_snapshot(node), read_snapshot(node)
    try:
        provenance = a_pass(node, conn)
        with pytest.raises(PolicyError):
            prove(provenance, other, unit)  # another snapshot is not the one `finish` checks
        prove(provenance, conn, unit)
        provenance.finish()
        with pytest.raises(PolicyError):
            prove(provenance, conn, unit)  # finished: nothing is proven after the check
        with pytest.raises(PolicyError):
            provenance.finish()
        closed = a_pass(node, conn)
        closed.close()
        with pytest.raises(PolicyError):
            prove(closed, conn, unit)
    finally:
        conn.close()
        other.close()
    outside = sqlite3.connect(f"file:{node.corpus.path}?mode=ro", uri=True)
    try:
        assert not outside.in_transaction
        with pytest.raises(PolicyError):
            prove(a_pass(node, outside), outside, unit)  # no snapshot: the dependencies would not share one
    finally:
        outside.close()


def test_the_end_check_must_see_the_generation_the_members_were_read_against(node, monkeypatch):
    conn = read_snapshot(node)
    try:
        provenance = a_pass(node, conn)
        prove(provenance, conn, node.corpus.units[0])
        original = provenance._service._check
        monkeypatch.setattr(provenance._service, "_check", lambda conn: original(conn) + 1)
        with pytest.raises(PolicyError):
            provenance.finish()
    finally:
        conn.close()


def test_the_end_check_must_read_the_snapshot_the_members_read(node):
    """A transaction ended and begun again between the members and `finish` would let the one check read a newer
    state -- a revocation already folded into rows and marker alike -- than the members were proven on."""
    wal(node)
    conn = read_snapshot(node)
    try:
        provenance = a_pass(node, conn)
        for unit in node.corpus.units:
            prove(provenance, conn, unit)
        conn.commit()
        revoke(node)
        conn.execute("BEGIN")
        assert conn.execute("SELECT state FROM ingest_provenance_enrollments").fetchone()[0] == "revoked"
        with pytest.raises(PolicyError):
            provenance.finish()
    finally:
        conn.close()


def test_a_pass_no_member_needed_checks_nothing(node, monkeypatch):
    checks = recorded_checks(monkeypatch)
    conn = read_snapshot(node)
    try:
        provenance = a_pass(node, conn)
        provenance.finish()
        assert provenance._service is None and checks == [] and provenance.seconds == {}
    finally:
        conn.close()


# -- the gate is released on every path ---------------------------------------------------------------------------

def _gate_is_free() -> bool:
    got = []

    def take():
        if write_gate._WRITE_LOCK.acquire(timeout=2):
            got.append(True)
            write_gate._WRITE_LOCK.release()
    worker = threading.Thread(target=take)
    worker.start()
    worker.join()
    return got == [True]


FAILURES = {
    "ok": None,
    "setup": lambda mp: mp.setattr(IngestProvenanceService, "__init__",
                                   lambda self, **kw: (_ for _ in ()).throw(PolicyError("synthetic_setup"))),
    "member": lambda mp: mp.setattr(reconciliation_provenance, "_existing_link",
                                    lambda *a, **kw: (_ for _ in ()).throw(PolicyError("synthetic_member"))),
    "check": lambda mp: mp.setattr(IngestProvenanceService, "_check_locked",
                                   lambda self, conn: (_ for _ in ()).throw(PolicyError("synthetic_check"))),
    "check_error": lambda mp: mp.setattr(IngestProvenanceService, "_check_locked",
                                         lambda self, conn: (_ for _ in ()).throw(OSError("synthetic"))),
    "generation": lambda mp: mp.setattr(reconciliation_provenance, "_source_generation", lambda conn: -1),
    "snapshot": None,  # the file replaced before the pass
}


@pytest.mark.parametrize("timing", ["off", "on"])
@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_the_gate_is_released_on_every_path(node, monkeypatch, failure, timing):
    if timing == "on":
        monkeypatch.setenv(FLAG, "true")
    if failure == "snapshot":
        replace_snapshot(node)
    elif FAILURES[failure] is not None:
        FAILURES[failure](monkeypatch)
    made = recorded_passes(monkeypatch)
    outcome = []

    def run():
        transport = search_timing.TransportTiming()
        try:
            with transport.active(), node.search.verification() as verified:
                own_check(node, verified, laps=transport.check_own_laps(), provenance_point="send_check_provenance")
            outcome.append("ok")
        except PolicyError as exc:
            outcome.append(exc.code)
        outcome.append(write_gate._WRITE_LOCK._is_owned())
    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=60)
    assert not worker.is_alive()
    assert outcome == (["ok", False] if failure == "ok" else ["search_index_stale", False])
    assert made and all(provenance._closed for provenance in made)
    assert _gate_is_free()


# -- IF-3 v1.4: where members goes, and the pass's two gate waits ------------------------------------------------

def _timed_search(node, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    message = relay_message(node, signed(node, payload=node.query, request_id="n3c-timing"), node.query, monkeypatch,
                            request_id="n3c-timing")

    def message_search():  # what Runtime.message_search does: report to the transport's timing
        timing = search_timing.for_adapter()
        node.search.observe = timing.observe if timing is not None else None
        return node.search
    search_transport.get_runtime().message_search = message_search
    return message


def within(part, whole, slack=0.05):
    return 0 <= float(part) <= float(whole) + slack


@pytest.mark.asyncio
async def test_index_load_and_send_check_split_members_and_time_the_passs_gate_waits(node, monkeypatch, caplog):
    full_send_check(monkeypatch)
    message = _timed_search(node, monkeypatch)
    socket = Socket()
    with caplog.at_level("INFO", logger=LOGGER):
        await search_transport.dispatch_message_search(socket, message)
    node.search.observe = None
    [frame] = [json.loads(value) for value in socket.sent]
    assert frame["status"] == "ok" and frame["payload"]["output"]["records"]
    stages = by_stage(parsed(caplog))
    [index_load], [recheck], [check] = stages["index_load"], stages["recheck"], stages["send_check"]
    assert "members_ms" not in index_load  # N5: index load checks the basis only
    for line in (recheck, check):
        members = line["members_ms"]
        assert within(line["dependencies_ms"], members) and within(line["dependency_boundary_ms"], line["dependencies_ms"])
        assert within(line["provenance_setup_ms"], line["dependencies_ms"])
        after_loop = float(line["dependencies_ms"]) + float(line["provenance_check_ms"]) + float(line["provenance_snapshot_ms"])
        assert within(after_loop, members)
        assert float(line["provenance_check_ms"]) > 0 and float(line["dependency_boundary_ms"]) > 0
    waits = {}
    for line in stages["gate_wait"]:
        waits.setdefault(line["point"], []).append(line["ms"])
    # One line per gate entry of the send check's pass; the recheck already holds the gate and writes none, and
    # index load no longer proves members (N5).
    assert len(waits["send_check_provenance"]) == len(waits["send_check_provenance_setup"]) == 1
    assert not [point for point in waits if point.startswith("index_load_provenance")]
    assert within(waits["send_check_provenance"][0], check["provenance_check_ms"])
    assert within(waits["send_check_provenance_setup"][0], check["provenance_setup_ms"])
    text = " ".join(record.getMessage() for record in caplog.records if record.name == LOGGER)
    canaries = ["n3c-timing", node.search_raw["binding"]["grant_id"], "actor-1", "client-2", dst.DATASET,
                enrollment_id(node), "canary"] + node.query["query"].split()
    canaries += [record["record_id"] for record in frame["payload"]["output"]["records"]]
    canaries += [unit.message_id for unit in node.corpus.units]
    assert not [canary for canary in canaries if canary in text]


def test_a_wait_at_the_end_check_leaves_members_as_its_own_line(node, monkeypatch, caplog):
    """The gate held by another thread when the pass's one `_check` asks for it: the wait is that line's, exactly."""
    monkeypatch.setenv(FLAG, "true")
    original = ExistingProvenancePass.finish
    holders = []

    def finish(self):
        holders.append(hold_gate(0.2))
        return original(self)
    monkeypatch.setattr(ExistingProvenancePass, "finish", finish)
    transport = search_timing.TransportTiming()
    laps = transport.check_own_laps()
    with caplog.at_level("INFO", logger=LOGGER), transport.active(), node.search.verification() as verified:
        own_check(node, verified, laps=laps, provenance_point="index_load_provenance")
    for holder in holders:
        holder.join()
    waits = [line for line in parsed(caplog) if line["stage"] == "gate_wait" and line["point"] == "index_load_provenance"]
    assert len(waits) == 1 and 150.0 <= waits[0]["ms"] <= laps["provenance_check"] * 1000 + 0.05


@pytest.mark.asyncio
async def test_timing_on_releases_exactly_what_timing_off_releases(node, monkeypatch):
    off = await relayed(node, monkeypatch, "n3c-off")
    message = _timed_search(node, monkeypatch)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    node.search.observe = None
    [on] = [json.loads(value) for value in socket.sent]
    assert off["status"] == on["status"] == "ok" and off["payload"]["output"]["records"]
    assert off["payload"]["output"] == on["payload"]["output"]


@pytest.mark.asyncio
async def test_the_attribution_script_reports_the_members_split_and_the_provenance_waits(node, monkeypatch, caplog,
                                                                                         tmp_path):
    from tests.permissions_v2.test_search_timing_attribution_script import cp_lines, load_script
    full_send_check(monkeypatch)
    message = _timed_search(node, monkeypatch)
    with caplog.at_level("INFO", logger=LOGGER):
        await search_transport.dispatch_message_search(Socket(), message)
    node.search.observe = None
    records = [record for record in caplog.records if record.name == LOGGER]
    node_log = tmp_path / "node.log"
    node_log.write_text("".join(json.dumps({"level": "INFO", "logger": LOGGER, "message": record.getMessage(),
                                            "timestamp": record.created}) + "\n" for record in records))
    transport = next(float(r.getMessage().split("elapsed_ms=")[1].split()[0]) for r in records
                     if "stage=transport_total" in r.getMessage())
    cp_log = tmp_path / "cp.log"
    cp_log.write_text("".join(cp_lines(search_timing.correlation_id("n3c-timing"), transport + 40.0)))
    out = tmp_path / "report.json"
    assert load_script().main(["--node-log", str(node_log), "--cp-log", str(cp_log), "--json", str(out)]) == 0
    report = json.loads(out.read_text())
    [row] = report["per_search"]
    parts = ("dependencies", "dependency_boundary", "provenance_setup", "provenance_check", "provenance_snapshot")
    assert set(parts) <= set(row["recheck_parts_ms"]) and not set(parts) & set(row["index_load_parts_ms"])
    assert {f"check_own.{part}" for part in parts} <= set(row["send_check_parts_ms"])
    assert set(row["gate_wait_provenance_ms"]) == {"send_check_provenance_setup", "send_check_provenance"}
    totals = report["totals"]
    assert totals["node_stages_ms"]["recheck.provenance_check"] == pytest.approx(
        row["recheck_parts_ms"]["provenance_check"])
    for point, ms in row["gate_wait_provenance_ms"].items():
        assert totals["gate_wait_by_point_ms"][point] == pytest.approx(ms)
