"""N3: many shares on one node. One change rebuilds one index, after its acknowledgement, off the write gate.

Ten search shares on one in-process node, each to a different invented person, over one invented corpus. An applied
change is acknowledged within its own gate hold and queues only the share it changed; the queue builds it on its own
thread, holding the gate only for its two brief steps, so a writer that arrives during the build does not wait for it.
A review change drops, in its own critical section, the indexes whose basis cannot see it (p2c-v1), and queues every
share most-read first. The refresh loop's restore after a protection change runs most-read first too, under the same
build slot, leaves a share the owner-change queue owes to it, and is settled by that queue's publish.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, owner
from topos.core.handlers import handle_control_plane_request
from topos.core.handlers import permissions_v2 as handlers
from topos.permissions_v2 import runtime as runtime_module
from topos.permissions_v2.canonical import digest
from topos.permissions_v2.index_rebuilds import IndexRebuilds, most_read_first
from topos.permissions_v2.ledger import utc_day
from topos.permissions_v2.protocol import MutationBody, sign_mutation, verify_ack
from topos.permissions_v2.refresh_loop import RefreshLoop, RefreshSettings, protection_sync
from topos.permissions_v2.registry import parse_policy
from topos.permissions_v2.search_index import SearchIndexService, index_path
from topos.principal import OWNER_APP, Principal
from topos.storage.db import write_gate

SHARES = [f"share-{number:02d}" for number in range(10)]


def share_policy(number: int, **options) -> dict:
    """Share `number`: the same rules to a different invented person on the same app."""
    version = options.pop("version", None)
    raw = mc.search_policy(grant=SHARES[number], actor=f"person-{number:02d}", client="client-2", **options)
    if version:
        raw["policy_version_id"] = version
    return raw


@pytest.fixture
def many(tmp_path, monkeypatch):
    # The node runs on the wall clock, as the owner hooks read it (`_forget_inactive_record_keys` takes its own).
    monkeypatch.setattr(mc, "NOW", int(time.time()))
    corpus = mc.build(tmp_path / "corpus", seed=31, counts={name: 1 for name in mc.KINDS} | {"clean_positive_C": 24})
    embed_corpus(corpus)
    node = Node(corpus, tmp_path / "node", search_raw=share_policy(0), now=mc.NOW)
    for number in range(1, len(SHARES)):
        node.activate(share_policy(number))
    states = node.rebuild()
    assert {grant: states[grant] for grant in SHARES} == {grant: "ready" for grant in SHARES}
    rebuilds = IndexRebuilds(ledger=node.ledger, root=node.index.root, index=lambda: node.index,
                             sync_protection=protection_sync(node.protocol))
    runtime = SimpleNamespace(protocol=node.protocol, record_keys_root=lambda: node.index.root,
                              message_search_index=lambda: node.index, index_rebuilds=lambda: rebuilds)
    monkeypatch.setattr(runtime_module, "get_runtime", lambda: runtime)
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED", "true")
    node.runtime = runtime
    yield node, rebuilds
    rebuilds.close()


def identities(node) -> dict:
    """Each share's index file identity (a publish replaces the file, so a new inode); None when absent."""
    found = {}
    for grant in SHARES:
        try:
            info = os.stat(index_path(node.index.root, grant))
            found[grant] = (info.st_ino, info.st_mtime_ns)
        except FileNotFoundError:
            found[grant] = None
    return found


def spy_builds(monkeypatch) -> list:
    built = []
    real = SearchIndexService._rebuild

    def rebuild(self, grant_id, *, now=None):
        built.append(grant_id)
        return real(self, grant_id, now=now)
    monkeypatch.setattr(SearchIndexService, "_rebuild", rebuild)
    return built


def slow_builds(monkeypatch, seconds: float) -> threading.Event:
    """Each build pauses `seconds` in its ungated phase (where qualification and embedding run on a real node)."""
    building = threading.Event()
    real = SearchIndexService._members

    def members(self, *args, **kwargs):
        building.set()
        time.sleep(seconds)
        return real(self, *args, **kwargs)
    monkeypatch.setattr(SearchIndexService, "_members", members)
    return building


def writer_during(event: threading.Event):
    """A writer that wants the node write gate as soon as `event` is set; its wait, in seconds."""
    waits = []

    def writer():
        if event.wait(10):
            began = time.perf_counter()
            with write_gate.with_db_write():
                waits.append(time.perf_counter() - began)
    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    return thread, waits


def signed_change(node, grant: str, *, generation: int, command_id: str, policy_raw=None, operation="activate"):
    """The control plane's signed mutation for one share: a new policy (activate) or its end (revoke)."""
    with node.ledger._transaction() as conn:
        node.protocol._sync_protection(conn)
        state = node.ledger._node(conn)
    if operation == "activate":
        policy = parse_policy(policy_raw).model_dump()
        version, policy_hash, capability = policy["policy_version_id"], digest(policy), policy["versions"]["capability"]
        binding = policy["binding"]
    else:
        policy = None
        with owner():
            current = node.ledger.authority_snapshot(grant, now=node.now[0])
        version, policy_hash, capability = current.policy_version_id, current.policy_hash, current.capability_version
        binding = {key: getattr(current, key) for key in ("environment_id", "node_id", "resource_id", "owner_id",
                                                          "actor_id", "client_id", "grant_id", "assignment_id")}
    authority = {**binding, "grant_generation": generation, "assignment_generation": generation,
                 "policy_version_id": version, "policy_hash": policy_hash, "capability_version": capability,
                 "protection_revision": state["protection_revision"], "node_epoch": state["epoch"] + 1}
    return sign_mutation(MutationBody.parse({
        "version": "topos-policy-mutation/v2", "kid": "cp-key", "issuer_id": "cp-issuer",
        "audience_id": node.ledger.identity.node_id, "command_id": command_id, "operation": operation,
        "expected_epoch": state["epoch"], "authority": authority, "policy": policy,
        "owner_authorization": {"actor_id": mc.OWNER_ID, "client_id": "owner-ui"},
        "issued_at": node.now[0], "expires_at": node.now[0] + 120}), node.cp_key)


async def send_change(node, change):
    """The control plane's relay of a signed change to the owner's node: (reply, seconds until it came back)."""
    started = time.perf_counter()
    reply = await handle_control_plane_request(
        {"id": change.command_id, "type": "permissions_v2_mutate", "payload": {"envelope": change.model_dump()}},
        principal=Principal(cls=OWNER_APP, channel="cp_relay", acting_user=mc.OWNER_ID))
    return reply, time.perf_counter() - started


def checked(node, reply, change):
    assert reply["status"] == "ok", reply
    return verify_ack(reply["payload"]["ack"], trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()},
                      issuer_id=node.ledger.identity.node_id, audience_id="cp-issuer", request=change,
                      now=int(time.time()))


def search(node, number: int):
    return node.search_request("roadmap review", k=5, grant_id=SHARES[number], actor=f"person-{number:02d}")


def ask(node, counts: dict) -> None:
    with sqlite3.connect(node.ledger.path) as conn:
        conn.executemany("INSERT INTO p2a_question_days VALUES (?,?,?)",
                         [(grant, utc_day(node.now[0]), count) for grant, count in counts.items()])


# -- one change, one index, after the ack, off the gate ----------------------------------------------------------

@pytest.mark.asyncio
async def test_one_change_among_ten_shares_rebuilds_one_index_after_the_ack_and_no_writer_waits(many, monkeypatch):
    node, rebuilds = many
    before = identities(node)
    built = spy_builds(monkeypatch)
    building = slow_builds(monkeypatch, 0.5)
    thread, waits = writer_during(building)
    change = signed_change(node, SHARES[3], generation=2, command_id="change-share-03",
                           policy_raw=share_policy(3, max_k=20, version="policy-share-03-v2"))

    reply, seconds = await send_change(node, change)

    ack = checked(node, reply, change)
    assert ack.outcome == "applied" and ack.expires_at - ack.issued_at == 120
    assert seconds < 5                                           # its life is 120 s; the build is not in it
    assert SHARES[3] in rebuilds.owed()                          # the ack left before the build ended
    assert building.wait(5)
    output, refused = search(node, 3)                            # the changed share, mid-build: refused
    assert output is None and refused is not None
    output, refused = search(node, 7)                            # another share: served from its own index
    assert refused is None and output["records"]
    assert rebuilds.wait_idle(30)
    thread.join(5)
    assert built == [SHARES[3]]                                  # one change, one build
    after = identities(node)
    assert {grant for grant in SHARES if before[grant] != after[grant]} == {SHARES[3]}
    assert waits and waits[0] < 0.25                             # never the 0.5 s the build took
    output, refused = search(node, 3)
    assert refused is None and output["records"]


@pytest.mark.asyncio
async def test_ending_one_share_drops_its_index_and_key_and_touches_no_other(many, monkeypatch):
    node, rebuilds = many
    before = identities(node)
    change = signed_change(node, SHARES[5], generation=2, command_id="end-share-05", operation="revoke")
    reply, _ = await send_change(node, change)
    ack = checked(node, reply, change)
    assert ack.outcome == "applied" and ack.state.grant_state == "revoked"
    assert rebuilds.wait_idle(30)
    assert not index_path(node.index.root, SHARES[5]).exists()
    assert node.index.keys.get(SHARES[5], create=False) is None
    after = identities(node)
    assert {grant for grant in SHARES if before[grant] != after[grant]} == {SHARES[5]}


@pytest.mark.asyncio
async def test_a_retried_change_queues_its_share_again(many, monkeypatch):
    node, rebuilds = many
    change = signed_change(node, SHARES[2], generation=2, command_id="change-share-02",
                           policy_raw=share_policy(2, version="policy-share-02-v2"))
    assert checked(node, (await send_change(node, change))[0], change).outcome == "applied"
    assert rebuilds.wait_idle(30)
    built = spy_builds(monkeypatch)
    assert checked(node, (await send_change(node, change))[0], change).outcome == "already_applied"
    assert rebuilds.wait_idle(30)
    assert built == [SHARES[2]]


# -- a review change: drop what the guard cannot see, rebuild every share most-read first ------------------------

def test_a_review_change_drops_the_unguarded_indexes_at_once_and_rebuilds_every_share_most_read_first(many, monkeypatch):
    node, rebuilds = many
    ask(node, {SHARES[7]: 5, SHARES[2]: 3, SHARES[5]: 3, SHARES[9]: 1})
    built = spy_builds(monkeypatch)
    slow_builds(monkeypatch, 0.05)
    with write_gate.with_db_write():                            # the review change's own critical section
        handlers._refresh_message_search(node.runtime)
        # p2c-v1's basis cannot see a review change: its indexes are gone before the gate is released.
        assert all(not index_path(node.index.root, grant).exists() for grant in SHARES)
    assert rebuilds.wait_idle(60)
    assert built == [SHARES[7], SHARES[2], SHARES[5], SHARES[9], SHARES[0], SHARES[1], SHARES[3], SHARES[4],
                     SHARES[6], SHARES[8]]
    assert all(index_path(node.index.root, grant).exists() for grant in SHARES)


def test_a_share_asked_for_during_its_own_build_is_built_once_more_and_a_queued_one_once(many, monkeypatch):
    node, rebuilds = many
    building = slow_builds(monkeypatch, 0.3)
    built = spy_builds(monkeypatch)
    rebuilds.request([SHARES[4]])
    assert building.wait(5)
    rebuilds.request([SHARES[4]])
    rebuilds.request([SHARES[4], SHARES[6]])
    rebuilds.request([SHARES[6]])
    assert rebuilds.wait_idle(30)
    assert built == [SHARES[4], SHARES[6], SHARES[4]]


def test_most_read_first_breaks_ties_by_grant_id():
    assert most_read_first(["c", "a", "b", "a"], {"b": 2, "c": 2}) == ["b", "c", "a"]


# -- the refresh loop's restore after a protection change ----------------------------------------------------------

def restore_loop(node, **options) -> RefreshLoop:
    settings = RefreshSettings(restore=True, debounce=0, min_interval=0, max_defer=0, backoff=10, max_backoff=40,
                               max_attempts=3, full_hours=None)
    return RefreshLoop(ledger=node.ledger, root=node.index.root, index=lambda: node.index, worker=None,
                       settings=settings, clock=lambda: node.now[0], sync_protection=protection_sync(node.protocol),
                       **options)


def protection_change(node) -> None:
    """An owner-only mark on an unrelated record: the protection clock moves, every index's basis with it."""
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) VALUES('conversation_messages','zz')")


def test_after_a_protection_change_every_index_goes_and_comes_back_one_at_a_time_most_read_first(many, monkeypatch):
    node, _ = many
    ask(node, {SHARES[8]: 9, SHARES[1]: 4})
    loop = restore_loop(node)
    loop.observe(node.index)
    protection_change(node)
    assert node.index.sweep(now=node.now[0]) == len(SHARES)    # the floor: every index dropped
    loop.observe(node.index)
    receipt = loop.run_pending()
    assert receipt.cause_classes == ["protection_changed"] and receipt.protection_synced
    assert [grant.grant_id for grant in receipt.grants] == [SHARES[8], SHARES[1]] + [
        grant for grant in SHARES if grant not in (SHARES[8], SHARES[1])]
    assert {grant.state for grant in receipt.grants} == {"ready"}


def test_the_restore_leaves_a_share_the_owner_change_queue_owes(many, monkeypatch):
    node, _ = many
    loop = restore_loop(node, owed=lambda: frozenset({SHARES[6]}))
    loop.observe(node.index)
    protection_change(node)
    node.index.sweep(now=node.now[0])
    loop.observe(node.index)
    receipt = loop.run_pending()
    assert SHARES[6] not in [grant.grant_id for grant in receipt.grants]
    assert SHARES[6] in loop._pending                             # still owed a restore if that build fails


def test_a_publish_after_a_drop_settles_the_restore_it_queued(many, monkeypatch):
    node, rebuilds = many
    loop = restore_loop(node)
    loop.observe(node.index)
    protection_change(node)
    node.index.sweep(now=node.now[0])
    loop.observe(node.index)
    assert set(loop._pending) == set(SHARES)
    rebuilds.request([SHARES[0], SHARES[1]])                     # the owner-change queue publishes two of them
    assert rebuilds.wait_idle(30)
    loop.observe(node.index)
    assert set(loop._pending) == set(SHARES[2:])
    receipt = loop.run_pending()
    assert [grant.grant_id for grant in receipt.grants] == SHARES[2:]


# -- A2A-4 Q5: a change of only the daily number re-stamps the index basis -------------------------------------------

def members_of(node, grant: str) -> list:
    with sqlite3.connect(index_path(node.index.root, grant).as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT opaque_id, event_at_us, doc_len, terms_json, sealed FROM members ORDER BY opaque_id")
        vectors = conn.execute("SELECT opaque_id, chunk_index, vector FROM vectors ORDER BY opaque_id, chunk_index")
        return [rows.fetchall(), vectors.fetchall()]


def basis_of_file(node, grant: str) -> dict:
    import json
    with sqlite3.connect(index_path(node.index.root, grant).as_uri() + "?mode=ro", uri=True) as conn:
        return json.loads(conn.execute("SELECT basis_json FROM meta").fetchone()[0])


@pytest.mark.asyncio
async def test_a_change_of_only_the_daily_number_restamps_the_index_and_builds_nothing(many, monkeypatch):
    node, rebuilds = many
    kept, before = members_of(node, SHARES[4]), identities(node)
    built = spy_builds(monkeypatch)
    change = signed_change(node, SHARES[4], generation=2, command_id="limit-share-04",
                           policy_raw={**share_policy(4, version="policy-share-04-limit"), "read_budget_per_day": 50})
    assert checked(node, (await send_change(node, change))[0], change).outcome == "applied"
    assert rebuilds.wait_idle(30)
    assert built == [] and rebuilds.results[-1][0] == "restamped"
    assert members_of(node, SHARES[4]) == kept                                # the same members, byte for byte
    basis = basis_of_file(node, SHARES[4])
    assert (basis["grant_generation"], basis["policy_hash"]) == (2, change.authority.policy_hash)
    after = identities(node)
    assert {grant for grant in SHARES if before[grant] != after[grant]} == {SHARES[4]}
    output, refused = search(node, 4)
    assert refused is None and output["records"]

    # Anything else the policy says is a build.
    change = signed_change(node, SHARES[4], generation=3, command_id="k-share-04",
                           policy_raw={**share_policy(4, max_k=10, version="policy-share-04-k"),
                                       "read_budget_per_day": 50})
    assert checked(node, (await send_change(node, change))[0], change).outcome == "applied"
    assert rebuilds.wait_idle(30)
    assert built == [SHARES[4]]


@pytest.mark.asyncio
async def test_a_daily_number_change_after_a_protection_change_is_a_build(many, monkeypatch):
    node, rebuilds = many
    protection_change(node)
    built = spy_builds(monkeypatch)
    change = signed_change(node, SHARES[4], generation=2, command_id="limit-after-protection",
                           policy_raw={**share_policy(4, version="policy-share-04-limit"), "read_budget_per_day": 50})
    assert checked(node, (await send_change(node, change))[0], change).outcome == "applied"
    assert rebuilds.wait_idle(30)
    assert built == [SHARES[4]] and rebuilds.results[-1][0] == "ready"
    output, refused = search(node, 4)
    assert refused is None and output["records"]


def test_a_light_change_is_the_daily_number_the_version_and_the_answers_mode_only():
    from topos.permissions_v2.search_index import light_change
    old = parse_policy(share_policy(1)).model_dump()
    assert light_change(old, {**old, "policy_version_id": "other", "read_budget_per_day": 7})
    assert light_change(old, {**old, "search": {**old["search"], "answers": "only"}})
    assert not light_change(old, {**old, "search": {**old["search"], "max_k": 3}})
    assert not light_change(old, {**old, "validity": {**old["validity"], "expires_at": old["validity"]["expires_at"] + 1}})
    assert not light_change(old, {**old, "rules": old["rules"][:1]})


# -- after a restart: the builds a restart lost --------------------------------------------------------------------

def test_at_start_every_active_share_with_no_index_is_built_most_read_first(many, monkeypatch):
    from topos.permissions_v2.refresh_loop import queue_missing_indexes
    from topos.permissions_v2.search_index import purge
    node, rebuilds = many
    ask(node, {SHARES[6]: 2})
    for grant in (SHARES[1], SHARES[6]):
        purge(node.index.root, grant)                            # acknowledged, then the node restarted unbuilt
    before = identities(node)
    built = spy_builds(monkeypatch)
    assert queue_missing_indexes(node.runtime) == [SHARES[6], SHARES[1]]
    assert rebuilds.wait_idle(30)
    assert built == [SHARES[6], SHARES[1]]
    after = identities(node)
    assert {grant for grant in SHARES if before[grant] != after[grant]} == {SHARES[1], SHARES[6]}
    assert queue_missing_indexes(node.runtime) == []             # nothing missing: nothing asked
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED")
    purge(node.index.root, SHARES[2])
    assert queue_missing_indexes(node.runtime) == []             # search off: nothing built


def test_the_startup_thread_asks_for_the_missing_indexes_before_the_loop_starts(many, monkeypatch):
    from topos.permissions_v2 import refresh_loop
    from topos.permissions_v2.search_index import purge
    node, rebuilds = many
    order = []
    node.runtime.refresh_loop = lambda: order.append(("loop", sorted(rebuilds.owed())))
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED", "true")
    slow_builds(monkeypatch, 0.3)
    purge(node.index.root, SHARES[8])
    assert refresh_loop.start_at_startup(delay=0)
    deadline = time.monotonic() + 10
    while not order and time.monotonic() < deadline:
        time.sleep(0.02)
    assert order == [("loop", [SHARES[8]])]                     # owed when the loop starts: the loop leaves it
    assert rebuilds.wait_idle(30) and index_path(node.index.root, SHARES[8]).exists()


# -- the two automatic rebuilders, together --------------------------------------------------------------------------

def test_a_publish_settles_only_a_restore_queued_for_a_drop(many):
    node, rebuilds = many
    loop = restore_loop(node)
    loop.observe(node.index)
    with loop._lock:                                            # facts moved: a build of an index that is still there
        loop._pending[SHARES[3]] = {"causes": {"facts_changed"}, "attempts": 0, "not_before": 0.0,
                                    "first_drop_at": node.now[0]}
    rebuilds.request([SHARES[3]])
    assert rebuilds.wait_idle(30)
    loop.observe(node.index)
    assert SHARES[3] in loop._pending                            # only a restore settles it


def test_the_queue_and_the_restore_never_build_at_once(many, monkeypatch):
    from topos.permissions_v2.search_index import purge
    node, rebuilds = many
    spans = []
    real = SearchIndexService._rebuild

    def rebuild(self, grant_id, *, now=None):
        began = time.monotonic()
        try:
            time.sleep(0.2)
            return real(self, grant_id, now=now)
        finally:
            spans.append((began, time.monotonic()))
    monkeypatch.setattr(SearchIndexService, "_rebuild", rebuild)
    loop = restore_loop(node)
    loop.observe(node.index)
    purge(node.index.root, SHARES[2])                            # a drop the restore will see
    loop.observe(node.index)
    rebuilds.request([SHARES[5]])                                # the queue starts its build at once
    restore = threading.Thread(target=loop.run_pending, daemon=True)
    restore.start()
    restore.join(30)
    assert rebuilds.wait_idle(30)
    assert len(spans) == 2
    (_first_began, first_ended), (second_began, _second_ended) = sorted(spans)
    assert first_ended <= second_began                           # one after the other, never both


# -- a re-stamp never crosses what the guard binds ---------------------------------------------------------------------

def tamper_basis(node, grant: str, **fields) -> None:
    import json
    path = index_path(node.index.root, grant)
    with sqlite3.connect(path) as conn:
        basis = json.loads(conn.execute("SELECT basis_json FROM meta").fetchone()[0])
        conn.execute("UPDATE meta SET basis_json=?", (json.dumps({**basis, **fields}, sort_keys=True,
                                                                  separators=(",", ":")),))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["protection_revision", "clock_generation", "entity_boundary_revision"])
async def test_an_index_whose_basis_moved_is_rebuilt_never_restamped(many, monkeypatch, field):
    node, rebuilds = many
    basis = basis_of_file(node, SHARES[4])
    moved = {"protection_revision": "0" * 64, "clock_generation": basis["clock_generation"] + 1,
             "entity_boundary_revision": "moved"}[field]
    tamper_basis(node, SHARES[4], **{field: moved})
    built = spy_builds(monkeypatch)
    change = signed_change(node, SHARES[4], generation=2, command_id=f"limit-{field}",
                           policy_raw={**share_policy(4, version="policy-share-04-limit"), "read_budget_per_day": 50})
    assert checked(node, (await send_change(node, change))[0], change).outcome == "applied"
    assert rebuilds.wait_idle(30)
    assert built == [SHARES[4]] and rebuilds.results[-1][0] == "ready"
    assert basis_of_file(node, SHARES[4])[field] == basis[field]


# -- where the owner hooks queue ---------------------------------------------------------------------------------------

def test_every_review_hook_queues_inside_its_own_gate_hold():
    """A review change drops what the guard cannot see before its gate is released: the refresh call sits inside the
    handler's `with with_db_write():` block, never after it (source shape, as the mutate hook's test pins)."""
    import inspect
    message = inspect.getsource(handlers.handle_permissions_v2_message_review)
    assert "\n            _refresh_message_search(runtime)\n" in message
    assert "\n        _refresh_message_search(runtime)\n" not in message
    evidence = inspect.getsource(handlers._handle_evidence)
    assert evidence.count("                _refresh_message_search(runtime)\n") == 2
    mutate = inspect.getsource(handlers._handle)
    assert mutate.count("                _refresh_message_search(runtime, [ack.receipt.authority.grant_id])\n") == 2
