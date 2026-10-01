"""Node-side search refresh (refresh_loop.py): N7 restore after a drift drop, RD2 catch-up, IF-5 I8 interests."""
import asyncio
import bisect
import json
import sqlite3
import stat
import time as _clock_time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_automatic_message_review import setup, answer
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from tests.permissions_v2.test_knowledge_search import node_for
from topos.permissions_v2 import automatic_review_worker as worker_module
from topos.permissions_v2.automatic_message_review import prepare, publish
from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.message_review_contract import AutomaticReviewRequest, AutomaticReviewStatus
from topos.permissions_v2.refresh_loop import (STATE_FILE, CatchUpReceipt, InterestRefreshReceipt, RefreshLoop,
                                               RefreshSettings, assessment_revisions, proof_digest, protection_sync)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def settings(**overrides):
    values = dict(restore=True, debounce=0, min_interval=0, max_defer=0, backoff=10, max_backoff=40, max_attempts=3,
                  full_hours=None)
    values.update(overrides)
    return RefreshSettings(**values)


def loop_for(node, clock, **overrides):
    return RefreshLoop(ledger=node.ledger, root=node.index.root, index=lambda: node.index, worker=None,
                       settings=settings(**overrides), clock=clock)


def reassess(node, identity):
    """A second machine assessment of the same message: the review digest moves, the member stays."""
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    with owner():
        prepared = prepare(resolver, reviews, identity)
        publish(resolver, reviews, prepared, answer(prepared), now=node.now[0])


def receipts(node):
    with node.ledger._transaction() as db:
        return [json.loads(row["receipt_json"]) for row in
                db.execute("SELECT receipt_json FROM p2a_system_actions ORDER BY recorded_at, action_id")]


# -- N7 on the real p2c-v3 index -------------------------------------------------------------

def test_restore_rebuilds_an_index_that_a_review_change_dropped(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    assert node.rebuild() == {"grant-search": "ready"}
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)                                  # the published set
    reassess(node, identity)
    assert node.index.sweep(now=node.now[0]) == 1            # today's drift drop
    output, refused = node.search_request("Synthetic message", k=10)
    assert output is None and refused is not None            # dark until something rebuilds

    loop.observe(node.index)
    receipt = loop.run_pending()

    assert receipt.cause_classes == ["review_changed"]
    assert [(g.grant_id, g.state, g.member_count) for g in receipt.grants] == [("grant-search", "ready", 1)]
    output, refused = node.search_request("Synthetic message", k=10)
    assert refused is None and len(output["records"]) == 1
    stored = receipts(node)
    assert [r["action"] for r in stored] == ["search_index_restore"]
    assert stored[0]["actor"] == "node_system" and stored[0]["grants"][0]["policy_hash"]
    assert "Synthetic" not in json.dumps(stored)              # counts and ids only, never content


def test_never_builds_an_index_the_owner_never_published(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)
    loop.observe(node.index)
    assert loop.run_pending() is None
    assert not list(node.index.root.glob("grant-*.db"))
    assert receipts(node) == []


def test_a_drop_across_a_restart_is_still_restored(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    loop_for(node, Clock(node.now[0])).observe(node.index)
    state = node.index.root / STATE_FILE
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    reassess(node, identity)
    node.index.sweep(now=node.now[0])

    restarted = loop_for(node, Clock(node.now[0]))
    restarted.observe(node.index)
    receipt = restarted.run_pending()

    assert receipt.cause_classes == ["restart_gap"]
    assert receipt.grants[0].state == "ready"


def test_a_restart_between_a_drop_and_its_restore_still_restores(legacy, tmp_path, monkeypatch):
    """Seen live on 28 Sep: a node restart lost the queued restore and left the grant dark."""
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    before = loop_for(node, Clock(node.now[0]), debounce=600)
    before.observe(node.index)
    reassess(node, identity)
    node.index.sweep(now=node.now[0])
    before.observe(node.index)                                # the drop is queued, not yet restored
    assert before.run_pending() is None
    assert len(json.loads((node.index.root / STATE_FILE).read_text())["names"]) == 1   # still owed

    restarted = loop_for(node, Clock(node.now[0]))
    restarted.observe(node.index)
    receipt = restarted.run_pending()
    assert receipt.cause_classes == ["restart_gap"] and receipt.grants[0].state == "ready"


def test_a_given_up_restore_is_no_longer_owed(tmp_path):
    clock = Clock(1000)
    index = FakeIndex(tmp_path, ["failed"])
    loop = ScheduledLoop(tmp_path, index, clock, max_attempts=1)
    drop(tmp_path, loop, index)
    from topos.permissions_v2.search_index import index_path
    assert json.loads((tmp_path / STATE_FILE).read_text())["names"] == [index_path(tmp_path, "g1").name]
    loop.run_pending()
    assert json.loads((tmp_path / STATE_FILE).read_text())["names"] == []


def test_starting_records_the_published_set_before_any_sweep(legacy, tmp_path, monkeypatch):
    """A fresh install has no state file; the first sweep must not be able to drop an unseen index."""
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    loop = loop_for(node, Clock(node.now[0]), tick=3600)
    loop.start(node.index)
    loop.close()
    reassess(node, identity)
    node.index.sweep(now=node.now[0])                         # the sweeper's first sweep drops it
    loop.observe(node.index)
    assert loop.run_pending().grants[0].state == "ready"


def owner_only(legacy, record_id="unrelated-record"):
    """An Off-limits mark on something unrelated: the protection clock moves, the member stays."""
    conn = legacy[1]
    columns = [r[1] for r in conn.execute("PRAGMA table_info(owner_only_records)")]
    values = {"canonical_table": "conversation_messages", "record_id": record_id, "created_at": 1,
              "reason": "synthetic restriction"}
    keys = [k for k in columns if k in values]
    conn.execute("INSERT INTO owner_only_records(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")",
                 [values[k] for k in keys])
    conn.commit()


def restore_loop(node, **kwargs):
    return RefreshLoop(ledger=node.ledger, root=node.index.root, index=lambda: node.index, worker=None,
                       settings=settings(), clock=Clock(node.now[0]), **kwargs)


def dropped_by_protection(legacy, node, loop):
    loop.observe(node.index)
    owner_only(legacy)
    assert node.index.sweep(now=node.now[0]) == 1
    loop.observe(node.index)


@pytest.mark.parametrize("synced", [False, True])
def test_a_protection_change_is_restored_only_after_the_node_syncs_it(legacy, tmp_path, monkeypatch, synced):
    """eb0a1f2a, lost on the way to main: after a protection clock move every restore was `stale` until a
    recipient request or a control-plane command synced the node, and it gave up after max_attempts."""
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    loop = restore_loop(node, sync_protection=protection_sync(node.protocol) if synced else None)
    dropped_by_protection(legacy, node, loop)
    receipt = loop.run_pending()
    assert "protection_changed" in receipt.cause_classes
    assert (receipt.grants[0].state, receipt.protection_synced) == (("ready", True) if synced else ("stale", False))


@pytest.mark.parametrize("synced", [False, True])
def test_until_the_owners_grant_sync_a_recipient_refuses_as_before(legacy, tmp_path, monkeypatch, synced):
    """The sync changes no policy. An envelope signed before the move refuses at admission exactly as it did
    without it (admission makes the same sync first, then binds the envelope to the current authority). With a
    fresh authority, which is what the owner's grant Sync gives the control plane, the synced restore answers
    at once; without it the index is still missing."""
    from tests.permissions_v2.message_search_harness import recipient
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    payload = {"query": "Synthetic message", "k": 10}
    signed_before = node._envelope(node.search_raw["binding"]["grant_id"], "permissions.v2.search", payload,
                                   "search-signed-before")
    loop = restore_loop(node, sync_protection=protection_sync(node.protocol) if synced else None)
    dropped_by_protection(legacy, node, loop)
    assert loop.run_pending().grants[0].state == ("ready" if synced else "stale")
    with recipient(), pytest.raises(PolicyError) as refused:
        node.search.dispatch(envelope=signed_before.model_dump(), payload=payload, request_id="search-signed-before")
    assert refused.value.code == "authority_binding"           # verify_envelope: its protection revision is old
    output, reason = node.search_request("Synthetic message", k=10)
    if synced:
        assert reason is None and len(output["records"]) == 1
    else:                                                     # the uniform refusal: the index is still gone
        assert (output, reason) == (None, "permission_denied") and not list(node.index.root.glob("grant-*.db"))


def test_a_failing_sync_never_stops_the_restore_or_raises(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()

    def rolled_back():
        raise PolicyError("protection_clock_rollback")       # the sync's own guard still refuses

    loop = restore_loop(node, sync_protection=rolled_back)
    dropped_by_protection(legacy, node, loop)
    receipt = loop.run_pending()
    assert (receipt.grants[0].state, receipt.protection_synced) == ("stale", False)


def test_the_runtime_gives_its_loop_the_protection_sync(legacy, tmp_path, monkeypatch):
    from topos.permissions_v2.runtime import Runtime
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED", "true")
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_ENABLED", raising=False)
    runtime = SimpleNamespace(_refresh=None, protocol=node.protocol, message_search_index=lambda: node.index,
                              automatic_message_reviews=None)
    loop = Runtime.refresh_loop(runtime)
    try:
        assert loop._sync_protection() is False                # nothing moved since the grant was synced
        owner_only(legacy)
        assert loop._sync_protection() is True and loop._sync_protection() is False
    finally:
        loop.close()


def test_a_protection_clock_move_is_a_proof_change_on_a_real_clock(legacy, tmp_path, monkeypatch):
    """eb0a1f2a's other half (a clock move reassesses the window) is proof_change's protection-clock part here:
    one mechanism, not two."""
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    loop = restore_loop(node)
    before = loop._proof_digest(node.index.resolver.path)
    owner_only(legacy)
    assert before is not None and loop._proof_digest(node.index.resolver.path) not in (None, before)


def test_a_revoked_grant_is_never_restored(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)
    with owner():
        node.ledger.revoke("grant-search", expected_epoch=node.epoch(), command_id="revoke-search")
    node.index.sweep(now=node.now[0])
    loop.observe(node.index)
    assert loop.run_pending() is None
    assert not list(node.index.root.glob("grant-*.db"))


def test_restore_off_by_default_does_nothing(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    loop = RefreshLoop(ledger=node.ledger, root=node.index.root, index=lambda: node.index, worker=None,
                       settings=RefreshSettings.from_env({}))
    loop.observe(node.index)
    reassess(node, identity)
    node.index.sweep(now=node.now[0])
    loop.observe(node.index)
    assert loop.run_pending() is None
    assert not (node.index.root / STATE_FILE).exists()


def test_system_action_receipts_need_the_owner_process(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    with owner(actor="recipient"), pytest.raises(PolicyError):
        node.ledger.record_system_action({"version": "x"}, now=1)


# -- scheduling, with the ledger and index replaced -------------------------------------------

class FakeIndex:
    def __init__(self, root, results):
        self.root, self.results, self.calls = root, list(results), []

    def rebuild(self, grant_id, *, now):
        self.calls.append(now)
        state = self.results.pop(0)
        if state == "ready":
            (self.root / f"grant-{grant_id}.db").write_bytes(b"")
        return {"state": state, "member_count": 1 if state == "ready" else 0}


class ScheduledLoop(RefreshLoop):
    """Scheduling only: one active grant, fixed signals, receipts kept in memory."""

    def __init__(self, root, index, clock, worker=None, owner="owner-1", **overrides):
        ledger = SimpleNamespace(identity=SimpleNamespace(owner_id=owner))
        super().__init__(ledger=ledger, root=root, index=lambda: index, worker=worker,
                         settings=settings(**overrides), clock=clock)
        self.recorded = []

    def _dropped_grants(self, now, names):
        return [name[len("grant-"):-len(".db")] for name in sorted(names)]

    def _active_grants(self, now):
        return []

    def _policy_hash(self, grant_id, now):
        return "a" * 64

    def _current_signals(self, service):
        return ("digest", ("clock", 1))

    def _record(self, receipt):
        self.recorded.append(receipt)


def drop(root, loop, index, grant="g1"):
    path = root / f"grant-{grant}.db"
    path.write_bytes(b"")
    loop.observe(index)
    path.unlink()
    loop.observe(index)


def test_drops_are_coalesced_and_rate_limited(tmp_path):
    clock = Clock(1000)
    index = FakeIndex(tmp_path, ["ready", "ready", "ready"])
    loop = ScheduledLoop(tmp_path, index, clock, debounce=30, min_interval=300)
    drop(tmp_path, loop, index, "g1")
    clock.now = 1010
    drop(tmp_path, loop, index, "g2")
    clock.now = 1025
    assert loop.run_pending() is None                         # still inside the debounce
    clock.now = 1031
    receipt = loop.run_pending()
    assert [g.grant_id for g in receipt.grants] == ["g1", "g2"] and len(index.calls) == 2
    loop.observe(index)                                       # the next sweep sees both republished
    (tmp_path / "grant-g1.db").unlink()
    loop.observe(index)
    clock.now = 1200
    assert loop.run_pending() is None                         # inside min_interval of the last pass
    clock.now = 1331
    assert [g.grant_id for g in loop.run_pending().grants] == ["g1"]


def test_failed_restore_backs_off_then_gives_up(tmp_path):
    clock = Clock(1000)
    index = FakeIndex(tmp_path, ["failed", "stale", "failed"])
    loop = ScheduledLoop(tmp_path, index, clock, backoff=10, max_attempts=3)
    drop(tmp_path, loop, index)
    assert loop.run_pending().grants[0].state == "failed"
    clock.now = 1009
    assert loop.run_pending() is None
    clock.now = 1010
    assert loop.run_pending().grants[0].state == "stale"
    clock.now = 1029
    assert loop.run_pending() is None                         # backoff doubled to 20 s
    clock.now = 1030
    assert loop.run_pending().grants[0].state == "failed"
    clock.now = 5000
    assert loop.run_pending() is None and len(index.calls) == 3   # dark until the owner acts, as before


class FakeWorker:
    def __init__(self):
        self.is_running, self.started, self.status_value, self.assessed_so_far = False, [], None, 0

    def running(self):
        return self.is_running

    def start_node_pass(self, request, *, now, ingested_after, max_assessed):
        self.started.append(dict(after=request.after, before=request.before, ingested_after=ingested_after,
                                 max_assessed=max_assessed))
        self.is_running = True

    def status(self):
        if self.status_value is None and self.is_running:
            return AutomaticReviewStatus(state="running", assessed=self.assessed_so_far)
        return self.status_value


def test_a_running_assessment_defers_a_restore_up_to_the_limit(tmp_path):
    clock = Clock(1000)
    index, worker = FakeIndex(tmp_path, ["ready"]), FakeWorker()
    loop = ScheduledLoop(tmp_path, index, clock, worker=lambda: worker, max_defer=600)
    drop(tmp_path, loop, index)
    worker.is_running = True
    clock.now = 1300
    assert loop.run_pending() is None
    clock.now = 1600
    assert loop.run_pending().grants[0].state == "ready"


class CatchUpLoop(ScheduledLoop):
    """Catch-up scheduling with the rules and proof state as plain values the test moves."""

    def __init__(self, *args, window=30 * 86400, new_rows=False, revisions=None, proof="proof-0", **kwargs):
        super().__init__(*args, **kwargs)
        self.window, self.new_rows = window, new_rows
        self.revisions, self.proof = ({"rules": "r0"} if revisions is None else revisions), proof

    def _window_seconds(self, now):
        return self.window

    def _new_ingest(self, path, high_water):
        return self.new_rows

    def _assessment_revisions(self):
        return self.revisions

    def _proof_digest(self, path):
        return self.proof


def finish(worker, **counts):
    worker.is_running = False
    worker.status_value = AutomaticReviewStatus(state="complete", **counts)


def test_catch_up_runs_a_full_pass_then_only_changed_conversations(tmp_path):
    T = 1_790_000_000
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, max_assessed=50,
                       catchup_interval=300, full_interval=86_400)
    loop.run_catchup()
    assert worker.started[-1] == dict(after=T - 30 * 86400, before=T, ingested_after=None, max_assessed=50)
    finish(worker, scanned=10, assessed=4, current=6)
    clock.now = T + 60
    receipt = loop.run_catchup()
    assert (receipt.cause_class, receipt.scope, receipt.assessed, receipt.budget_exhausted) == \
        ("startup_backlog", "full_window", 4, False)
    state = json.loads((tmp_path / STATE_FILE).read_text())
    assert state["last_full_pass_at"] == T and state["ingest_high_water"] == T

    clock.now = T + 400
    loop.run_catchup()                                        # nothing new: no pass
    assert len(worker.started) == 1
    loop.new_rows = True
    clock.now = T + 800
    loop.run_catchup()
    assert worker.started[-1]["ingested_after"] == T
    finish(worker, scanned=2, assessed=2)
    clock.now = T + 810
    assert loop.run_catchup().scope == "changed_conversations"

    clock.now = T + 86_400
    loop.run_catchup()
    assert worker.started[-1]["ingested_after"] is None      # the daily full reconciliation
    worker.is_running = False
    worker.status_value = AutomaticReviewStatus(state="complete", assessed=50)
    clock.now += 10
    receipt = loop.run_catchup()
    assert receipt.cause_class == "daily_reconciliation" and receipt.budget_exhausted
    assert json.loads((tmp_path / STATE_FILE).read_text())["last_full_pass_at"] == T   # not advanced


def test_quiet_ticks_read_nothing_between_intervals(tmp_path):
    T = 1_790_000_000
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, catchup_interval=300)
    reads = []
    loop._window_seconds = lambda now: reads.append(now) or 30 * 86400
    loop.run_catchup()
    finish(worker)
    for offset in (5, 60, 150, 299):                          # the 5 s ticks inside one interval
        clock.now = T + offset
        loop.run_catchup()
    assert reads == [T]                                       # the ledger was read once, not per tick
    clock.now = T + 310
    loop.run_catchup()
    assert reads == [T, T + 310]


def test_the_daily_full_pass_waits_for_the_night_window(tmp_path):
    import time as _time
    evening = int(_time.mktime((2026, 9, 29, 20, 0, 0, 0, 0, -1)))
    clock, worker = Clock(evening), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, catchup_interval=300,
                       full_hours=(2, 6))
    loop._state = {"version": "topos-search-refresh-state/v1", "names": [], "ingest_high_water": evening - 60,
                   "last_full_pass_at": evening - 86_400, "assessment_revisions": loop.revisions,
                   "proof_digest": loop.proof, "continuation": None}
    loop.run_catchup()
    assert worker.started == []                               # a day old, but it is 20:00
    clock.now = int(_time.mktime((2026, 9, 30, 3, 0, 0, 0, 0, -1)))
    loop.run_catchup()
    assert worker.started[-1]["ingested_after"] is None      # 03:00: the full reconciliation


def test_the_end_of_a_pass_restores_without_waiting_for_the_debounce(tmp_path):
    T = 1_790_000_000
    clock, worker = Clock(T), FakeWorker()
    index = FakeIndex(tmp_path, ["ready"])
    index.resolver = SimpleNamespace(path=tmp_path / "canonical.db")
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, debounce=600, max_defer=3600)
    loop.run_catchup()                                        # the pass starts and its assessments drop the index
    drop(tmp_path, loop, index)
    clock.now = T + 40
    assert loop.run_pending() is None                         # still assessing, inside the debounce
    finish(worker, assessed=3)
    loop.run_catchup()                                        # the pass ends
    assert loop.run_pending().grants[0].state == "ready"      # at once: every drop of that pass is in


def test_catch_up_never_interferes_with_the_owners_pass(tmp_path):
    clock, worker = Clock(100_000), FakeWorker()
    worker.is_running = True
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True)
    assert loop.run_catchup() is None and worker.started == []


def test_catch_up_needs_restore_and_is_off_by_default():
    assert not RefreshSettings.from_env({}).enabled
    only_catchup = RefreshSettings.from_env({"TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_ENABLED": "true"})
    assert not only_catchup.catchup and not only_catchup.enabled
    both = RefreshSettings.from_env({"TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED": "true",
                                     "TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_ENABLED": "true",
                                     "TOPOS_PERMISSIONS_V2_INDEX_RESTORE_MIN_INTERVAL_SECONDS": "5"})
    assert both.catchup and both.min_interval == 60           # floored


def test_after_sweep_never_raises(tmp_path):
    loop = ScheduledLoop(tmp_path, SimpleNamespace(), Clock(1))

    def broken(service):
        raise RuntimeError("ledger unavailable")

    loop.observe = broken
    loop.after_sweep(SimpleNamespace())                       # an exception would end the sweep thread


# -- the worker's node pass ------------------------------------------------------------------

def request_for(legacy):
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    now = canonical_utc_microseconds(legacy[1].execute("SELECT event_at FROM conversation_messages").fetchone()[0]) // 1000000 + 1
    return AutomaticReviewRequest(after=now - 86400, before=now)


def test_node_pass_only_rechecks_conversations_with_new_rows_and_never_refreshes(legacy):
    conn = legacy[1]
    conn.execute("ALTER TABLE conversation_messages ADD COLUMN ingested_at TEXT")
    conn.execute("UPDATE conversation_messages SET ingested_at='2026-09-20T12:00:00Z'")
    conn.commit()
    resolver, reviews, _, _ = setup(legacy)
    refreshed = []

    async def classify(prepared):
        return answer(prepared)

    worker = AutomaticReviewWorker(resolver, reviews, classifier=classify, refresh=lambda: refreshed.append(1))
    import calendar
    ingested = calendar.timegm((2026, 9, 20, 12, 0, 0))
    with owner():
        asyncio.run(worker._process(request_for(legacy), refresh=False, ingested_after=ingested))
        assert worker.status().scanned == 0                  # nothing ingested after the high-water mark
        asyncio.run(worker._process(request_for(legacy), refresh=False, ingested_after=ingested - 60))
        assert worker.status().scanned == 1 and worker.status().assessed == 1
    assert refreshed == []


def test_node_pass_stops_at_its_model_budget(monkeypatch):
    rows = [("m1", "s", "d"), ("m2", "s", "d"), ("m3", "s", "d")]
    calls = []

    class Reviews:
        def _db(self):
            import contextlib
            return contextlib.nullcontext(None)

        def _current_in(self, db, key):
            return None

    resolver = SimpleNamespace(binding=None, _identity=lambda *a: a[1])
    worker = AutomaticReviewWorker(resolver, Reviews(), classifier=lambda p: asyncio.sleep(0, result="labels"))
    monkeypatch.setattr(worker, "_page", lambda table, after, request, ingested_after=None:
                        [row for row in rows if row[0] > after] if table == "conversation_messages" else [])
    monkeypatch.setattr(worker_module, "prepare", lambda resolver, reviews, identity: {"id": identity})
    monkeypatch.setattr(worker_module, "is_current", lambda previous, prepared: False)
    monkeypatch.setattr(worker_module, "machine_key", lambda identity: identity)
    monkeypatch.setattr(worker_module, "publish", lambda *a, **k: calls.append(a[2]["id"]))
    asyncio.run(worker._process(AutomaticReviewRequest(after=0, before=1), refresh=False, max_assessed=2))
    assert calls == ["m1", "m2"]


def test_node_pass_rejects_a_bad_budget(legacy):
    resolver, reviews, _, _ = setup(legacy)
    worker = AutomaticReviewWorker(resolver, reviews)
    with owner(), pytest.raises(PolicyError, match="message_review_budget_invalid"):
        worker.start_node_pass(request_for(legacy), max_assessed=0)


# -- the causes beyond new ingest: rules, proof, budget ----------------------------------------

def daytime(hour=14):
    return int(_clock_time.mktime((2026, 9, 30, hour, 0, 0, 0, 0, -1)))


def settled(loop, now, **extra):
    """A node whose last full pass completed an hour ago under the loop's current rules and proof."""
    loop._state = {"version": "topos-search-refresh-state/v1", "names": [], "ingest_high_water": now - 3600,
                   "last_full_pass_at": now - 3600, "assessment_revisions": loop.revisions, "proof_digest": loop.proof,
                   "continuation": None, **extra}


def stored_state(root):
    return json.loads((root / STATE_FILE).read_text())


def test_changed_rules_run_a_full_pass_at_once_outside_the_night_window(tmp_path):
    """OD-54 staled every AI-chat assessment at an install; nothing re-assessed them until 02:00."""
    T = daytime()
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6))
    settled(loop, T)
    loop.revisions = {"rules": "r1"}                          # the install changed a rule
    loop.run_catchup()
    assert worker.started == [dict(after=T - 30 * 86400, before=T, ingested_after=None, max_assessed=500)]
    finish(worker, scanned=80, assessed=62)
    clock.now = T + 400
    receipt = loop.run_catchup()
    assert (receipt.cause_class, receipt.scope, receipt.budget_exhausted) == ("revision_change", "full_window", False)
    state = stored_state(tmp_path)
    assert state["assessment_revisions"] == {"rules": "r1"} and state["ingest_high_water"] == T
    assert state["last_full_pass_at"] == T - 3600             # not the nightly pass: that one still runs at 02:00
    clock.now = T + 800
    loop.run_catchup()
    assert len(worker.started) == 1                           # recorded: the same rules never run it twice


def test_unchanged_rules_and_proof_across_a_restart_start_no_pass(tmp_path):
    T = daytime()
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    first = RealRulesLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6))
    first.run_catchup()                                       # the backlog records what it ran under
    finish(worker, scanned=5, assessed=1)
    clock.now = T + 60
    assert first.run_catchup().cause_class == "startup_backlog"
    assert stored_state(tmp_path)["assessment_revisions"] == assessment_revisions()

    restarted = RealRulesLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6))
    clock.now = T + 400
    assert restarted.run_catchup() is None and len(worker.started) == 1


def test_a_state_file_from_before_the_rules_were_recorded_runs_one_full_pass(tmp_path):
    """The install that brings this loop may itself change a rule; with no record, that cannot be told."""
    T = daytime()
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    (tmp_path / STATE_FILE).write_text(json.dumps({"version": "topos-search-refresh-state/v1", "names": [],
                                                   "ingest_high_water": T - 600, "last_full_pass_at": T - 3600}))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6))
    loop.run_catchup()
    assert worker.started[-1]["ingested_after"] is None
    finish(worker)
    clock.now = T + 10
    assert loop.run_catchup().cause_class == "revision_change"
    assert stored_state(tmp_path)["names"] == [] and stored_state(tmp_path)["proof_digest"] == "proof-0"


def test_unreadable_rules_start_no_rule_pass_and_never_raise(tmp_path, monkeypatch):
    from topos.permissions_v2 import shadow_labeler_local
    T = daytime()
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = RealRulesLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6))
    settled(loop, T, assessment_revisions={"rules": "unknown"})
    monkeypatch.setattr(shadow_labeler_local, "RUBRIC_PATH", tmp_path / "missing.md")   # a reinstall under the node
    assert loop._assessment_revisions() is None
    loop.run_catchup()
    assert worker.started == []


def test_a_new_capture_receipt_runs_a_proof_change_pass(tmp_path):
    """A receipt proves rows ingested long before it; the ingest high-water mark never sees them."""
    from tests.permissions_v2.interest_fixtures import OWNER, attest_app, install, open_db
    T = daytime()
    conn = open_db(tmp_path / "canonical.db")
    install(conn)
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = RealProofLoop(tmp_path, index, clock, worker=lambda: worker, owner=OWNER, catchup=True, full_hours=(2, 6))
    settled(loop, T, proof_digest=loop._proof_digest(tmp_path / "canonical.db"))
    loop.run_catchup()
    assert worker.started == []                               # nothing moved
    attest_app(conn)                                          # the owner attests the browser plugin's rows
    conn.close()
    clock.now = T + 300
    loop.run_catchup()
    assert worker.started[-1]["ingested_after"] is None
    finish(worker, scanned=3)
    clock.now = T + 330
    receipt = loop.run_catchup()
    assert (receipt.cause_class, receipt.scope) == ("proof_change", "full_window")
    assert stored_state(tmp_path)["proof_digest"] == loop._proof_digest(tmp_path / "canonical.db")
    assert stored_state(tmp_path)["last_full_pass_at"] == T - 3600
    clock.now = T + 700
    loop.run_catchup()
    assert len(worker.started) == 1


def test_an_unreadable_canonical_database_moves_no_proof_and_never_raises(tmp_path):
    T = daytime()
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "missing" / "canonical.db"))
    loop = RealProofLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6))
    settled(loop, T)
    assert loop._proof_digest(index.resolver.path) is None
    loop.run_catchup()
    assert worker.started == []


@pytest.fixture()
def canonical(tmp_path):
    from tests.permissions_v2.interest_fixtures import install, open_db
    conn = open_db(tmp_path / "canonical.db")
    install(conn)
    yield conn
    conn.close()


def _proof(conn):
    from tests.permissions_v2.interest_fixtures import OWNER
    conn.commit()
    return proof_digest(conn, owner_id=OWNER)


def _attest(conn):
    from tests.permissions_v2.interest_fixtures import attest_app
    return attest_app(conn)


def _revoke(conn):
    from topos.permissions_v2 import capture_receipts as cr
    from tests.permissions_v2.interest_fixtures import OWNER
    receipt = cr.receipts(conn, owner_id=OWNER)[0]["receipt_id"]
    cr.revoke(conn, owner_id=OWNER, receipt_id=receipt, now=1_700_000_100)


def _ai_chat_receipt(conn):
    from topos.permissions_v2 import ai_chat_capture
    from tests.permissions_v2.interest_fixtures import OWNER
    ai_chat_capture.install(conn)
    conn.execute("INSERT INTO ai_chat_capture_receipts (receipt_id, version, owner_id, source_id, app_id, statement, "
                 "preview_digest, row_count, attested_at, revoked_at, dataset_id) VALUES "
                 "('acr-1','v','" + OWNER + "','chatgpt_ui_conversation','app','s','d',4,1700000000,NULL,'ds')")


def _attestation(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS permissions_v2_identity_attestations (sequence INTEGER PRIMARY KEY, "
                 "entry_id TEXT)")
    conn.execute("INSERT INTO permissions_v2_identity_attestations (entry_id) VALUES ('entry-1')")


def _retire_install(conn):
    conn.execute("UPDATE source_runtime_installs SET is_active=0, status='superseded'")


def _second_install(conn):
    from tests.permissions_v2.interest_fixtures import install
    install(conn, source="grow_journal")


def _posture(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS user_ingestion_sources (source_id TEXT, dataset_id TEXT, posture TEXT, "
                 "enabled INTEGER, last_sync_at TEXT)")
    conn.execute("INSERT INTO user_ingestion_sources VALUES ('imessage','ds-1','personal',1,NULL)")


def _enrollment(conn):
    from topos.permissions_v2.ingest_provenance import _SCHEMA
    conn.execute(_SCHEMA["ingest_provenance_enrollments"])
    conn.execute("INSERT INTO ingest_provenance_enrollments VALUES ('enr-1','{}','ds-1',1,'active',4,'a',1700000000,'uds')")


@pytest.mark.parametrize("setup,change", [(None, _attest), (_attest, _revoke), (None, _ai_chat_receipt),
                                          (None, _attestation), (None, _retire_install), (None, _second_install),
                                          (None, _posture), (None, _enrollment)])
def test_the_proof_digest_moves_with_each_proof_the_loop_watches(canonical, setup, change):
    if setup is not None:
        setup(canonical)
    before = _proof(canonical)
    change(canonical)
    assert _proof(canonical) != before


def test_a_refresh_moves_an_enrollment_and_a_clock_move_moves_the_proof(canonical, monkeypatch):
    from topos.permissions_v2 import protection_clock
    _enrollment(canonical)
    before = _proof(canonical)
    canonical.execute("UPDATE ingest_provenance_enrollments SET revision=2")   # RD8: same row, next revision
    assert _proof(canonical) != before
    monkeypatch.setattr(protection_clock, "clock_state", lambda conn, **kw: ("clock-1", 7))
    seven = _proof(canonical)
    monkeypatch.setattr(protection_clock, "clock_state", lambda conn, **kw: ("clock-1", 8))   # an Off-limits edit
    assert _proof(canonical) != seven


def test_the_proof_digest_ignores_ingest_sync_receipts_and_other_owners(canonical):
    from tests.permissions_v2.interest_fixtures import at, visit
    _attest(canonical)
    before = _proof(canonical)
    canonical.execute("UPDATE source_runtime_installs SET updated_at='2026-09-30T12:00:00Z', failure_reason='x'")
    visit(canonical, 900, at(9, 12))                          # new rows are the new-ingest pass's job
    canonical.execute("INSERT INTO capture_receipts (receipt_id, version, owner_id, canonical_table, source_id, app_id, "
                      "statement, preview_digest, row_count, attested_at, revoked_at, dataset_id) VALUES "
                      "('cap-other','v','someone-else','activity_events','browser_visits','app','s','d',0,1,NULL,'ds')")
    assert _proof(canonical) == before


@pytest.mark.parametrize("origin,new_rows,ingested_after", [
    ("revision_change", False, None), ("daily_reconciliation", False, None), ("new_ingest", True, "high_water")])
def test_a_pass_that_spends_its_budget_continues_one_interval_later_until_within_budget(tmp_path, origin, new_rows,
                                                                                        ingested_after):
    T = daytime(3) if origin == "daily_reconciliation" else daytime()
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6), max_assessed=50,
                       catchup_interval=300, new_rows=new_rows)
    settled(loop, T, last_full_pass_at=T - (86_400 if origin == "daily_reconciliation" else 3600))
    if origin == "revision_change":
        loop.revisions = {"rules": "r1"}
    high_water = loop._state["ingest_high_water"]
    expected_after = high_water if ingested_after == "high_water" else None
    loop.run_catchup()
    assert worker.started[-1]["ingested_after"] == expected_after
    finish(worker, scanned=400, assessed=50)                  # stopped at its budget
    clock.now = T + 3 * 3600                                  # a long pass: past the night window now
    first = loop.run_catchup()
    assert (first.cause_class, first.budget_exhausted) == (origin, True)
    assert stored_state(tmp_path)["continuation"]["scope"] == first.scope

    clock.now += 299
    loop.run_catchup()
    assert len(worker.started) == 1                           # one interval of rest: the restore serves meanwhile
    clock.now += 1
    loop.run_catchup()
    assert len(worker.started) == 2 and worker.started[-1]["ingested_after"] == expected_after
    finish(worker, scanned=400, assessed=50)
    clock.now += 900
    assert loop.run_catchup().cause_class == "budget_continuation"

    restarted = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6),
                            max_assessed=50, revisions=loop.revisions, new_rows=False)
    clock.now += 300
    restarted.run_catchup()                                   # owed across a restart
    assert len(worker.started) == 3 and worker.started[-1]["ingested_after"] == expected_after
    finish(worker, scanned=400, assessed=12)
    clock.now += 600
    last = restarted.run_catchup()
    assert (last.cause_class, last.budget_exhausted) == ("budget_continuation", False)
    state = stored_state(tmp_path)
    assert state["continuation"] is None and state["ingest_high_water"] == clock.now - 600
    # Only the nightly pass's own continuation counts as the nightly pass.
    assert state["last_full_pass_at"] == ((clock.now - 600) if origin == "daily_reconciliation" else T - 3600)
    if origin == "revision_change":
        assert state["assessment_revisions"] == {"rules": "r1"}
    clock.now += 900
    restarted.run_catchup()
    assert len(worker.started) == 3


def test_a_backlog_that_spends_its_budget_is_continued_and_then_counts_as_the_backlog(tmp_path):
    T = daytime()
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6), max_assessed=50)
    loop.run_catchup()
    finish(worker, assessed=50)
    clock.now = T + 600
    assert loop.run_catchup().cause_class == "startup_backlog"
    assert stored_state(tmp_path)["last_full_pass_at"] is None
    clock.now = T + 900
    loop.run_catchup()
    assert worker.started[-1]["ingested_after"] is None
    finish(worker, assessed=10)
    clock.now = T + 1000
    assert loop.run_catchup().cause_class == "budget_continuation"
    state = stored_state(tmp_path)
    assert (state["last_full_pass_at"], state["assessment_revisions"], state["continuation"]) == (T + 900,
                                                                                              {"rules": "r0"}, None)


@pytest.mark.parametrize("owed", [{"scope": "changed_conversations", "ingested_after": "x", "origin": "new_ingest"},
                                  {"scope": "full_window", "ingested_after": 5, "origin": "daily_reconciliation"},
                                  {"scope": "full_window", "ingested_after": None, "origin": "owner_pass"}, ["x"]])
def test_a_malformed_continuation_in_the_state_file_is_dropped(tmp_path, owed):
    """A start_node_pass that refuses its own arguments would fail every interval for good."""
    (tmp_path / STATE_FILE).write_text(json.dumps({"version": "topos-search-refresh-state/v1", "names": [],
                                                   "ingest_high_water": 1, "last_full_pass_at": 1,
                                                   "assessment_revisions": ["not", "a", "dict"], "proof_digest": 7,
                                                   "continuation": owed}))
    loop = CatchUpLoop(tmp_path, SimpleNamespace(), Clock(1))
    state = loop._load_state()
    assert (state["continuation"], state["assessment_revisions"], state["proof_digest"]) == (None, None, None)


def test_a_rule_change_during_a_continuation_starts_its_own_pass(tmp_path):
    T = daytime()
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6), max_assessed=50)
    settled(loop, T)
    loop.proof = "proof-1"
    loop.run_catchup()
    finish(worker, assessed=50)
    clock.now = T + 600
    assert loop.run_catchup().cause_class == "proof_change"
    loop.revisions = {"rules": "r1"}
    clock.now = T + 900
    loop.run_catchup()
    finish(worker, assessed=3)
    clock.now = T + 1000
    receipt = loop.run_catchup()
    assert receipt.cause_class == "revision_change" and not receipt.budget_exhausted
    state = stored_state(tmp_path)
    assert (state["assessment_revisions"], state["proof_digest"], state["continuation"]) == ({"rules": "r1"}, "proof-1",
                                                                                            None)


def test_nothing_starts_while_the_owners_pass_runs(tmp_path):
    """Changed rules, changed proof and an owed continuation all wait; the owner's pass is never touched."""
    T = daytime()
    clock, worker = Clock(T), FakeWorker()
    worker.is_running = True                                  # the owner's own pass, started by the owner
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6))
    settled(loop, T, continuation={"scope": "full_window", "ingested_after": None, "origin": "daily_reconciliation",
                                   "revisions": {"rules": "r0"}, "proof": "proof-0"})
    loop.revisions, loop.proof = {"rules": "r1"}, "proof-1"
    for offset in range(0, 3600, 300):
        clock.now = T + offset
        loop.step()
    assert worker.started == [] and loop.recorded == []
    worker.is_running = False                                 # the owner's pass ends
    clock.now = T + 3600
    loop.step()
    assert len(worker.started) == 1 and loop._pass["cause"] == "revision_change"


# -- the restore and the node's own pass -------------------------------------------------------

def test_the_nodes_own_pass_holds_the_restore_until_it_ends(tmp_path):
    """A rebuild under the node's pass is invalidated by its next assessment: it could only end `stale`."""
    T = 1_790_000_000
    clock, worker = Clock(T), FakeWorker()
    index = FakeIndex(tmp_path, ["ready"])
    index.resolver = SimpleNamespace(path=tmp_path / "canonical.db")
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, debounce=30, max_defer=600)
    loop.run_catchup()
    drop(tmp_path, loop, index)                               # its first assessment dropped the index
    for offset in range(5, 1300, 5):                          # it keeps assessing, far past max_defer
        clock.now = T + offset
        worker.assessed_so_far = offset // 5
        loop.step()
    assert index.calls == []
    finish(worker, assessed=260)
    clock.now = T + 1305
    loop.step()
    assert index.calls == [T + 1305]                          # its end restores at once


def test_a_node_pass_that_publishes_nothing_for_max_defer_no_longer_holds_the_restore(tmp_path):
    T = 1_790_000_000
    clock, worker = Clock(T), FakeWorker()
    index = FakeIndex(tmp_path, ["ready"])
    index.resolver = SimpleNamespace(path=tmp_path / "canonical.db")
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, debounce=30, max_defer=600)
    loop.run_catchup()
    drop(tmp_path, loop, index)
    worker.assessed_so_far = 1
    clock.now = T + 5
    loop.step()                                               # its last assessment
    clock.now = T + 604
    loop.step()
    assert index.calls == []
    clock.now = T + 606                                       # stalled, or only scanning current rows
    loop.step()
    assert index.calls == [T + 606]


# -- how long a grant is dark around a daytime pass (production timings) -----------------------

class TimedWorker:
    """The node's passes on a clock: pass n publishes `calls[n]` assessments (0 once the list runs out), one every
    `per_call` seconds from its start."""

    def __init__(self, clock, calls, per_call):
        self.clock, self.calls, self.per_call, self.starts = clock, list(calls), per_call, []

    def _count(self, n):
        return self.calls[n] if n < len(self.calls) else 0

    def _times(self, n):
        return [self.starts[n] + self.per_call * (k + 1) for k in range(self._count(n))]

    def publishes(self):
        return sorted(at for n in range(len(self.starts)) for at in self._times(n))

    def end(self):
        n = len(self.starts) - 1
        return self.starts[n] + self.per_call * self._count(n) + 1

    def running(self):
        return bool(self.starts) and self.clock.now < self.end()

    def start_node_pass(self, request, *, now, ingested_after, max_assessed):
        self.starts.append(now)

    def status(self):
        done = sum(at <= self.clock.now for at in self._times(len(self.starts) - 1))
        return AutomaticReviewStatus(state="running" if self.running() else "complete", assessed=done)


class TimedIndex:
    """A rebuild takes `seconds` on the loop's thread and starts over when an assessment lands inside it,
    three tries in all, as SearchIndexService._rebuild does. An index is current from the moment it is
    published until the next assessment moves the review digest."""

    def __init__(self, root, clock, worker, seconds):
        self.root, self.clock, self.worker, self.seconds = root, clock, worker, seconds
        self.resolver = SimpleNamespace(path=root / "canonical.db")
        self.built_at_version, self.ready, self.stale = 0, [], []

    def version(self, at):
        return bisect.bisect_right(self.worker.publishes(), at)

    def rebuild(self, grant_id, *, now):
        start = self.clock.now
        for _attempt in range(3):
            finish = start + self.seconds
            if self.version(finish) == self.version(start):
                self.clock.now = finish
                (self.root / f"grant-{grant_id}.db").write_bytes(b"")
                self.built_at_version = self.version(start)
                self.ready.append(finish)
                return {"state": "ready", "member_count": 1}
            start = finish
        self.clock.now = start
        self.stale.append(start)
        return {"state": "stale", "member_count": 0}


def dark_around_passes(root, *, calls, per_call, rebuild=37, after=1800, loop_class=None, **timings):
    """Seconds a grant cannot be searched around daytime passes that start with a rule change at T0, on the
    node's own timings: the loop every 5 s, the sweeper every 10 s, the restore's debounce, interval, deferral
    and backoff as configured. `calls` is each pass's model calls; a pass of `max_assessed` is continued."""
    T0 = daytime()
    clock = Clock(T0)
    worker = TimedWorker(clock, calls, per_call)
    index = TimedIndex(root, clock, worker, rebuild)
    values = dict(debounce=30, min_interval=300, max_defer=600, backoff=300, max_backoff=3600, max_attempts=8,
                  full_hours=(2, 6), catchup_interval=300, max_assessed=500)
    values.update(timings)
    loop = (loop_class or CatchUpLoop)(root, index, clock, worker=lambda: worker, catchup=True, **values)
    settled(loop, T0)
    loop.revisions = {"rules": "r1"}                          # an install changed a rule: a full pass, now
    path = root / "grant-g1.db"
    path.write_bytes(b"")
    loop.observe(index)
    second, next_tick = T0, T0
    end = T0 + sum(calls) * per_call + (len(calls) + 1) * 400 + after
    while second < end:
        if second % 10 == 0:                                  # the sweeper drops what drifted
            clock.now = second
            if path.exists() and index.built_at_version != index.version(second):
                path.unlink()
            loop.observe(index)
        if second >= next_tick:
            clock.now = second
            loop.step()
            next_tick = max(second, clock.now) + 5            # a rebuild held the loop's thread
        second += 1
    publishes = worker.publishes()
    current = [(T0, publishes[0])] + [(ready, next((p for p in publishes if p > ready), end)) for ready in index.ready]
    serving = sum(max(0, min(stop, end) - start) for start, stop in current)
    back = next((ready for ready in index.ready if ready > publishes[-1]), None)
    last_end = max(start + per_call * count + 1 for start, count in zip(worker.starts, calls))
    return {"passes": len(worker.starts), "dark_seconds": (end - T0) - serving,
            "back_after_last_pass": None if back is None else back - last_end,
            "dark_span": None if back is None else back - publishes[0],
            "rebuilds_ready": len(index.ready), "rebuilds_stale": len(index.stale)}


@pytest.mark.parametrize("calls,per_call", [([62], 4), ([62], 6), ([500], 4)])
def test_a_daytime_pass_darkens_the_grant_for_the_pass_plus_one_rebuild(tmp_path, calls, per_call):
    """The measurement for WS0 (drop-on-drift unchanged): dark from the pass's first assessment until one
    rebuild after its end; no rebuild is wasted under the pass, so nothing backs off."""
    seen = dark_around_passes(tmp_path, calls=calls, per_call=per_call)
    assert seen["rebuilds_stale"] == 0 and seen["rebuilds_ready"] == 1
    assert 0 < seen["back_after_last_pass"] <= 5 + 37        # the next tick, then one rebuild
    assert seen["dark_span"] == seen["dark_seconds"] <= calls[0] * per_call + 5 + 37   # one stretch, no flicker


def test_a_continued_pass_leaves_the_grant_serving_between_its_passes(tmp_path):
    seen = dark_around_passes(tmp_path, calls=[500, 500, 120], per_call=4)
    assert seen["passes"] == 3 and seen["rebuilds_stale"] == 0 and seen["rebuilds_ready"] == 3
    assert seen["dark_span"] - seen["dark_seconds"] >= 2 * (300 - 37 - 5)   # served through each rest


# -- IF-5 I8: interests stored and their labels assessed ---------------------------------------

class Canonical:
    """The resolver's read snapshot and the review store's opt-outs over a plain file (no protection clock)."""

    def __init__(self, path, opt_outs=frozenset()):
        self.path, self.opt_outs = path, opt_outs
        self.resolver = self.reviews = self

    @contextmanager
    def _read(self, *, gated=True):
        assert gated is False                                 # the build reads a snapshot outside the gate
        conn = sqlite3.connect(Path(self.path).as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row                         # as EvidenceResolver._read hands it out
        try:
            conn.execute("BEGIN")
            yield conn, "floor"
        finally:
            conn.close()

    @contextmanager
    def _db(self):
        yield None

    def _opt_outs_in(self, db):
        return self.opt_outs


@pytest.fixture()
def browsing(tmp_path):
    from tests.permissions_v2.interest_fixtures import attest_app, cluster, install, month_of_visits, open_db
    conn = open_db(tmp_path / "canonical.db")
    install(conn)
    attest_app(conn)
    cluster(conn, "tc_hobby", "sourdough / baking / starter")
    month_of_visits(conn, 0, 5, [3, 9, 17])                   # August: qualifies
    month_of_visits(conn, 100, 5, [1, 5, 19], month=9)        # September, the current month: qualifies
    cluster(conn, "tc_ride", "cycling / touring")
    month_of_visits(conn, 200, 5, [2, 8, 16], cluster_id="tc_ride")
    conn.commit()
    yield tmp_path / "canonical.db"
    conn.close()


@pytest.fixture()
def model(monkeypatch, browsing):
    """The local model, replaced: it answers 'hobbies, none, none', and checks that no lock is held while it runs."""
    from topos.permissions_v2 import interest_review as ir
    from topos.storage.db.write_gate import db_write_lock
    calls = []

    async def assess(prepared, *, transport=None):
        assert not db_write_lock()._is_owned()                # never under the node write gate
        probe = sqlite3.connect(str(browsing), timeout=0)     # nor under SQLite's write lock
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()
        probe.close()
        calls.append(prepared["label_revision"])
        return ir.apply_floors(ir.parse_assessment({"domains": ["hobbies"], "sensitivity": "none",
                                                    "protected_content": "none"}, prepared["label_revision"]),
                               prepared["input"])

    monkeypatch.setattr(ir, "assess", assess)
    return calls


def interest_loop(root, path, clock, worker, **overrides):
    from tests.permissions_v2.interest_fixtures import OWNER
    (root / "index").mkdir(exist_ok=True)
    return CatchUpLoop(root / "index", Canonical(path), clock, worker=lambda: worker, owner=OWNER, catchup=True,
                       **overrides)


def stored_interests(path):
    conn = sqlite3.connect(str(path))
    try:
        objects = conn.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type='browsing_interest' "
                               "AND valid_to IS NULL").fetchone()[0]
        labels = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='interest_label_assessments'").fetchone()[0]
        assessed = conn.execute("SELECT COUNT(*) FROM interest_label_assessments").fetchone()[0] if labels else 0
        return objects, assessed
    finally:
        conn.close()


class Interests(Canonical):
    """The interest stand-in, plus the index service's rebuild, recorded."""

    def __init__(self, path, root, **kwargs):
        super().__init__(path, **kwargs)
        self.root, self.rebuilds = root, []

    def rebuild(self, grant_id, *, now):
        self.rebuilds.append(grant_id)
        return {"state": "ready", "member_count": 1}


class InterestGrantsLoop(CatchUpLoop):
    """g-int signs interests and has an index here; g-unbuilt signs them and has none; g-msg does not sign them."""

    def _active_grants(self, now):
        from tests.permissions_v2.test_interest_index import policy as signed
        return [("g-int", None, signed()), ("g-unbuilt", None, signed()),
                ("g-msg", None, signed(result_types=("message",)))]


def interest_grants_loop(tmp_path, browsing, clock, **overrides):
    from tests.permissions_v2.interest_fixtures import OWNER
    from topos.permissions_v2.search_index import index_path
    root = tmp_path / "index"
    root.mkdir(exist_ok=True)
    for grant_id in ("g-int", "g-msg"):
        index_path(root, grant_id).write_bytes(b"")
    service = Interests(browsing, root)
    loop = InterestGrantsLoop(root, service, clock, worker=lambda: FakeWorker(), owner=OWNER, catchup=True,
                              debounce=30, **overrides)
    return loop, service


def test_changed_interests_queue_one_rebuild_of_the_grants_that_sign_them(tmp_path, browsing, model):
    """WS0 on Lane C's finding: a new interest or a newly assessed label moves no index basis, so nothing dropped
    or rebuilt the index; new interests waited for an unrelated rebuild."""
    from tests.permissions_v2.interest_fixtures import NOW_US, at, cluster, visit
    T = NOW_US // 1_000_000
    clock = Clock(T)
    loop, service = interest_grants_loop(tmp_path, browsing, clock, interests=True)
    first = loop.run_interests()
    assert (first.inserted, first.assessed, first.rebuild_requested) == (3, 2, 1) and set(loop._pending) == {"g-int"}
    clock.now = T + 29
    assert loop.run_pending() is None and service.rebuilds == []   # the restore's own debounce
    clock.now = T + 30
    assert loop.run_pending().cause_classes == ["interest_changed"] and service.rebuilds == ["g-int"]

    clock.now = T + 3600
    quiet = loop.run_interests()                              # nothing changed: nothing requested
    assert (quiet.inserted, quiet.closed, quiet.assessed, quiet.rebuild_requested) == (0, 0, 0, 0)
    assert loop._pending == {}

    conn = sqlite3.connect(str(browsing))                     # a cluster-month newly qualifies
    cluster(conn, "tc_new", "birdwatching / owls")
    for n, day in enumerate((2, 8, 16, 2, 8)):
        visit(conn, 700 + n, at(8, day), cluster_id="tc_new")
    conn.commit()
    conn.close()
    clock.now = T + 7200
    grown = loop.run_interests()
    assert (grown.inserted, grown.assessed, grown.rebuild_requested) == (1, 1, 1)
    clock.now = T + 7230
    loop.run_pending()
    assert service.rebuilds == ["g-int", "g-int"]             # exactly one more


def test_with_the_interest_flag_off_no_rebuild_is_ever_requested(tmp_path, browsing, model):
    from tests.permissions_v2.interest_fixtures import NOW_US
    T = NOW_US // 1_000_000
    clock = Clock(T)
    loop, service = interest_grants_loop(tmp_path, browsing, clock, interests=False)
    for offset in (0, 40, 3700, 7300):
        clock.now = T + offset
        loop.step()
    assert loop._pending == {} and service.rebuilds == [] and model == []


def test_the_interest_hook_needs_its_flag_and_the_catch_up():
    on = {"TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED": "true", "TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_ENABLED": "true"}
    flag = {"TOPOS_PERMISSIONS_V2_INTEREST_SOURCES": "true"}
    assert not RefreshSettings.from_env(on).interests
    assert RefreshSettings.from_env({**on, **flag}).interests
    assert not RefreshSettings.from_env({"TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED": "true", **flag}).interests


def test_with_the_interest_flag_off_the_loop_never_touches_interests(tmp_path, browsing, model, monkeypatch):
    from tests.permissions_v2.interest_fixtures import NOW_US
    from topos.permissions_v2 import interest_family as fam
    monkeypatch.setattr(fam, "persist", lambda *a, **k: pytest.fail("persist ran with the flag off"))
    T = NOW_US // 1_000_000
    clock, worker = Clock(T), FakeWorker()
    loop = interest_loop(tmp_path, browsing, clock, worker, interests=False)
    loop.run_catchup()
    finish(worker, scanned=1)
    for offset in (5, 4000, 8000):
        clock.now = T + offset
        loop.step()
    assert loop.run_interests() is None and model == []
    assert not any(isinstance(r, InterestRefreshReceipt) for r in loop.recorded)
    assert stored_interests(browsing) == (0, 0)


def test_interests_are_stored_and_labelled_after_a_pass_within_its_budget(tmp_path, browsing, model):
    from tests.permissions_v2.interest_fixtures import NOW_US
    T = NOW_US // 1_000_000
    clock, worker = Clock(T), FakeWorker()
    loop = interest_loop(tmp_path, browsing, clock, worker, interests=True, max_assessed=50)
    loop.run_catchup()                                        # the backlog pass
    assert loop.run_interests() is None                       # never beside a pass
    finish(worker, scanned=60, assessed=49)
    assert loop.run_interests() is None                       # nor before the loop has seen the pass end
    clock.now = T + 30
    loop.step()                                               # the pass ends; one model call is left
    receipt = loop.recorded[-1]
    assert isinstance(receipt, InterestRefreshReceipt) and receipt.cause_class == "after_pass"
    assert (receipt.inserted, receipt.pending, receipt.assessed, receipt.budget, receipt.budget_exhausted) == \
        (3, 2, 1, 1, True)
    assert len(model) == 1 and stored_interests(browsing) == (3, 1)
    assert receipt.state == "complete" and "sourdough" not in json.dumps(receipt.model_dump())

    clock.now = T + 600
    assert loop.run_interests() is None                       # not due again inside its interval
    clock.now = T + 30 + 3600
    again = loop.run_interests()                              # nothing else ran: its own interval, full budget
    assert (again.cause_class, again.budget, again.pending, again.assessed, again.unchanged, again.inserted) == \
        ("interval", 50, 1, 1, 3, 0)
    assert len(model) == 2 and stored_interests(browsing) == (3, 2)


def test_a_cluster_the_owner_opted_out_of_is_neither_stored_nor_assessed(tmp_path, browsing, model):
    from tests.permissions_v2.interest_fixtures import NOW_US, OWNER
    from topos.permissions_v2 import interest_family as fam
    T = NOW_US // 1_000_000
    (tmp_path / "index").mkdir()
    loop = CatchUpLoop(tmp_path / "index", Canonical(browsing, opt_outs=frozenset({fam.opt_out_key("tc_ride")})),
                       Clock(T), worker=lambda: FakeWorker(), owner=OWNER, catchup=True, interests=True)
    receipt = loop.run_interests()
    assert (receipt.inserted, receipt.pending, receipt.assessed) == (2, 1, 1) and len(model) == 1


def test_an_exhausted_pass_leaves_the_labels_no_model_call(tmp_path, browsing, model):
    from tests.permissions_v2.interest_fixtures import NOW_US
    T = NOW_US // 1_000_000
    clock, worker = Clock(T), FakeWorker()
    loop = interest_loop(tmp_path, browsing, clock, worker, interests=True, max_assessed=50)
    loop.run_catchup()
    finish(worker, assessed=50)
    clock.now = T + 30
    loop.step()
    receipt = loop.recorded[-1]
    assert (receipt.cause_class, receipt.budget, receipt.assessed, receipt.inserted) == ("after_pass", 0, 0, 3)
    assert model == []


def test_the_owners_pass_starting_stops_the_label_assessments(tmp_path, browsing, model, monkeypatch):
    from tests.permissions_v2.interest_fixtures import NOW_US
    from topos.permissions_v2 import interest_review as ir
    T = NOW_US // 1_000_000
    clock, worker = Clock(T), FakeWorker()
    loop = interest_loop(tmp_path, browsing, clock, worker, interests=True)
    answer = ir.assess

    async def then_the_owner_starts(prepared, *, transport=None):
        labels = await answer(prepared, transport=transport)
        worker.is_running = True                              # the owner starts their own pass meanwhile
        return labels

    monkeypatch.setattr(ir, "assess", then_the_owner_starts)
    receipt = loop.run_interests()
    assert (receipt.state, receipt.assessed, receipt.pending) == ("cancelled", 1, 2)
    assert worker.started == []
    clock.now = T + 7200
    assert loop.run_interests() is None                       # and nothing starts while it runs


def test_a_vocabulary_change_while_the_model_runs_refuses_the_label(tmp_path, browsing, model, monkeypatch):
    from tests.permissions_v2.interest_fixtures import NOW_US
    from topos.permissions_v2 import interest_review as ir
    T = NOW_US // 1_000_000
    clock, worker = Clock(T), FakeWorker()
    loop = interest_loop(tmp_path, browsing, clock, worker, interests=True)
    answer = ir.assess

    async def while_an_off_limits_name_is_added(prepared, *, transport=None):
        labels = await answer(prepared, transport=transport)
        writer = sqlite3.connect(str(browsing))
        writer.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
                       "rebuild_state) VALUES ('bh-' || hex(randomblob(4)),'','Tamsin Orrery','tamsin orrery','complete')")
        writer.commit()
        writer.close()
        return labels

    monkeypatch.setattr(ir, "assess", while_an_off_limits_name_is_added)
    receipt = loop.run_interests()
    assert (receipt.assessed, receipt.unresolved) == (0, 2) and stored_interests(browsing)[1] == 0


def test_a_failing_model_ends_the_label_run_after_three(tmp_path, browsing, monkeypatch):
    from tests.permissions_v2.interest_fixtures import NOW_US, cluster, month_of_visits
    from topos.permissions_v2 import interest_review as ir
    conn = sqlite3.connect(str(browsing))
    for n in range(3):
        cluster(conn, f"tc_more{n}", f"gardening {n} / soil")
        month_of_visits(conn, 300 + 10 * n, 5, [2, 8, 16], cluster_id=f"tc_more{n}")
    conn.commit()
    conn.close()
    calls = []

    async def unreachable(prepared, *, transport=None):
        calls.append(1)
        raise ConnectionError("model host down")

    monkeypatch.setattr(ir, "assess", unreachable)
    T = NOW_US // 1_000_000
    loop = interest_loop(tmp_path, browsing, Clock(T), FakeWorker(), interests=True)
    receipt = loop.run_interests()
    assert (receipt.state, receipt.pending, receipt.unresolved, len(calls)) == ("failed", 5, 3, 3)


# -- receipts and their readers -----------------------------------------------------------------

def _catch_up(cause, state="complete"):
    return CatchUpReceipt(version="topos-node-system-action/v1", action="message_assessment_catchup",
                          actor="node_system", cause_class=cause, scope="full_window", window_after=1, window_before=2,
                          started_at=3, finished_at=4, state=state, scanned=5, assessed=6, current=7, withheld=8,
                          unresolved=0, budget_exhausted=False)


def test_receipts_with_the_new_causes_stay_readable_by_every_reader(tmp_path):
    import importlib
    import sys
    from topos.permissions_v2.canonical import canonical_bytes
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    gc, dc = importlib.import_module("grant_census"), importlib.import_module("daily_census")
    interest = InterestRefreshReceipt(version="topos-node-system-action/v1", action="interest_refresh",
                                      actor="node_system", cause_class="after_pass", started_at=10, finished_at=11,
                                      state="failed", inserted=0, closed=0, unchanged=0, pending=2, assessed=0,
                                      unresolved=3, budget=3, budget_exhausted=False, rebuild_requested=0)
    receipts = [_catch_up(cause) for cause in ("revision_change", "proof_change", "budget_continuation")] + [interest]
    ledger = tmp_path / "copy" / "permissions-v2" / "ledger.db"
    ledger.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(ledger))
    conn.execute("CREATE TABLE p2a_system_actions (action_id TEXT PRIMARY KEY, recorded_at INTEGER NOT NULL, "
                 "receipt_json TEXT NOT NULL)")
    for n, receipt in enumerate(receipts):                    # exactly as PolicyLedger.record_system_action stores it
        conn.execute("INSERT INTO p2a_system_actions VALUES (?,?,?)",
                     (f"sys-{n}", 100 + n, canonical_bytes(dict(receipt.model_dump())).decode("ascii")))
    conn.commit()
    stored = [json.loads(row[0]) for row in conn.execute("SELECT receipt_json FROM p2a_system_actions ORDER BY recorded_at")]
    conn.close()
    assert [type(r).model_validate(s) for r, s in zip(receipts, stored)] == receipts
    jobs = gc.job_state(tmp_path / "copy", 200)
    assert jobs["receipts"] == 4
    assert jobs["last"]["message_assessment_catchup"]["cause_class"] == "budget_continuation"
    assert jobs["last"]["interest_refresh"]["state"] == "failed"
    alerts = dc.diff({"job_state": jobs}, None)["alerts"]
    assert {"code": "refresh_failed", "action": "interest_refresh"} in alerts


def test_assessment_revisions_move_with_each_rule_they_name(monkeypatch):
    from topos.permissions_v2 import automatic_message_review as amr
    from topos.permissions_v2 import interest_review as ir
    journal = {"TOPOS_PERMISSIONS_V2_JOURNAL_SOURCES": "true"}
    base, with_journal = assessment_revisions(env={}), assessment_revisions(env=journal)
    assert json.loads(json.dumps(base)) == base == assessment_revisions(env={})   # stable through the state file
    assert with_journal != base                               # turning a family on is a rule change
    assert assessment_revisions(env={}, interests=True) != base
    checks = [({}, False, lambda: monkeypatch.setitem(amr.CONTEXT_VERSIONS, "ai_chat_messages", "context/v4")),
              ({}, False, lambda: monkeypatch.setattr(amr, "MODEL_REVISION", "0" * 64)),
              ({}, False, lambda: monkeypatch.setattr(amr, "FLOORS_VERSION", "message-semantic-floors/v3")),
              (journal, False, lambda: monkeypatch.setattr(amr, "JOURNAL_FLOORS_VERSION", "journal-entry-floors/v3")),
              (journal, False, lambda: monkeypatch.setitem(amr.JOURNAL_CONTEXT, "version", "context/v3")),
              ({}, True, lambda: monkeypatch.setattr(ir, "FLOORS_VERSION", "interest-label-floors/v3"))]
    for env, interests, change in checks:
        before = assessment_revisions(env=env, interests=interests)
        change()
        assert assessment_revisions(env=env, interests=interests) != before
        monkeypatch.undo()


def test_naming_the_journal_context_rule_leaves_its_revision_unchanged():
    from topos.permissions_v2.automatic_message_review import _journal_context
    from topos.permissions_v2.canonical import digest
    revision, _ = _journal_context(None, SimpleNamespace(terms={"b"}, handles={"a"}))
    assert revision == digest({"version": "message-classifier-context/v2", "family": "journal_entry/v1", "context": [],
                               "protected_terms": ["a", "b"]})


class RealRulesLoop(CatchUpLoop):
    _assessment_revisions = RefreshLoop._assessment_revisions


class RealProofLoop(CatchUpLoop):
    _proof_digest = RefreshLoop._proof_digest
