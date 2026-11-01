"""The owner's request path survives an index rebuild and a derive storm (core/owner_requests.py).

Three pieces, each reproduced here with a fake slow rebuild and pinned:

1. An owner frame (``signal_*``, ``truth_*``, ``calibration_*``, ``get_device_info``) runs its database reads on
   the owner executor, which rebuilds and embedding jobs never use: with every worker of the loop's default pool
   parked in rebuild work, the frame still answers within its budget, and the same measurement on a frame outside
   the path fails (so the pool really was full).
2. ``get_device_info`` is on the path and answers under the same conditions; its storage snapshot holds no thread
   (single-flight, awaited: pinned in tests/topos/test_device_info_snapshot_single_flight.py).
3. ``pace`` yields a rebuild thread while an owner frame is pending, bounded, never on a loop thread or under the
   write gate; the owner-change rebuild queue consults it between shares, and the real share-index build consults
   it between batches, on the ungated snapshot only.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import topos.core.handlers as hub
from topos.core import owner_requests, state
from topos.core.handlers import handle_control_plane_request
from topos.core.handlers.common import run_db_read
from topos.principal import OWNER_APP, Principal
from topos.storage.db.write_gate import with_db_write

OWNER = Principal(cls=OWNER_APP, channel="uds")
#: What an owner frame may take while a rebuild holds the default pool. The doors themselves answer in
#: milliseconds; the budget is generous for a loaded CI host.
BUDGET_SECONDS = 2.0
OWNER_FRAMES = {
    "calibration_catalog": {},
    "truth_claim_cards": {"limit": 5, "unlabelled_only": True},
    "signal_data_health": {},
}


@pytest.fixture(autouse=True)
def _fresh_counts():
    owner_requests._reset_for_tests()
    yield
    owner_requests._reset_for_tests()


@pytest.fixture
def database(tmp_path, monkeypatch):
    """A connection every handler resolves; which thread asked for it is recorded by name."""
    conn = sqlite3.connect(str(tmp_path / "node.sqlite"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, body TEXT)")
    conn.commit()
    asked_on: list[str] = []

    def resolve():
        asked_on.append(threading.current_thread().name)
        return conn

    monkeypatch.setattr(hub, "get_db_connection", resolve)
    monkeypatch.setattr(state, "get_db_connection", resolve)
    try:
        yield asked_on
    finally:
        conn.close()


class ParkedPool:
    """A fake slow rebuild: a default pool of ``workers`` threads, every one parked in rebuild work until released."""

    def __init__(self, workers: int = 2):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rebuild-pool")
        self.workers = workers
        self.release = threading.Event()
        self.parked = 0
        self.lock = threading.Lock()
        self.tasks: list[asyncio.Task] = []

    async def __aenter__(self):
        asyncio.get_running_loop().set_default_executor(self.pool)

        def rebuild_work():
            with self.lock:
                self.parked += 1
            self.release.wait(30)

        self.tasks = [asyncio.create_task(asyncio.to_thread(rebuild_work)) for _ in range(self.workers)]
        deadline = time.monotonic() + 5
        while self.parked < self.workers:                   # every worker is in rebuild work now
            assert time.monotonic() < deadline, "the fake rebuild never filled the pool"
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc):
        self.release.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.pool.shutdown(wait=True)


# -- 1. the owner executor ------------------------------------------------------------------------------------------

def test_the_path_is_a_closed_list_by_prefix_and_name():
    assert owner_requests.is_owner_request("get_device_info")
    assert owner_requests.is_owner_request("TRUTH_CLAIM_CARDS")
    assert owner_requests.is_owner_request("calibration_answer")
    assert owner_requests.is_owner_request("signal_entity_graph")
    for other in ("query", "get_table_rows", "permissions_v2_message_search", "healthcheck", "", None):
        assert not owner_requests.is_owner_request(other)


@pytest.mark.parametrize("msg_type", sorted(OWNER_FRAMES))
@pytest.mark.asyncio
async def test_an_owner_frame_answers_within_budget_while_a_fake_rebuild_holds_the_default_pool(msg_type, database):
    async with ParkedPool(workers=2):
        message = {"id": "req-1", "type": msg_type, "payload": OWNER_FRAMES[msg_type]}
        started = time.monotonic()
        response = await asyncio.wait_for(handle_control_plane_request(message, principal=OWNER), BUDGET_SECONDS)
        elapsed = time.monotonic() - started
    assert isinstance(response, dict) and response.get("id") == "req-1" and response.get("status") == "ok", response
    assert elapsed < BUDGET_SECONDS
    assert database, "the handler never reached the database: this proves nothing"
    assert all(name.startswith("owner-request") for name in database), database
    assert owner_requests.pending() == 0 and owner_requests.handled() == 1


@pytest.mark.asyncio
async def test_the_same_measurement_fails_for_a_frame_outside_the_path(database):
    """``get_table_count`` is off-loop too, but not the owner's path: with the pool parked it waits for a worker."""
    async with ParkedPool(workers=2) as parked:
        message = {"id": "req-2", "type": "get_table_count", "payload": {"table_name": "messages"}}
        task = asyncio.create_task(handle_control_plane_request(message, principal=OWNER))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.5)
        assert not database and owner_requests.handled() == 0
        parked.release.set()
        response = await asyncio.wait_for(task, 10)
    assert isinstance(response, dict) and response.get("id") == "req-2"
    assert database and all(name.startswith("rebuild-pool") for name in database), database


@pytest.mark.asyncio
async def test_run_db_read_takes_the_owner_executor_only_inside_an_owner_request(database):
    def where(conn):
        assert conn is not None
        return threading.current_thread().name

    with owner_requests.owner_request("truth_claim_cards"):
        assert owner_requests.in_owner_request() and owner_requests.pending() == 1
        assert (await run_db_read(where)).startswith("owner-request")
    assert not owner_requests.in_owner_request() and owner_requests.pending() == 0
    assert not (await run_db_read(where)).startswith("owner-request")
    with owner_requests.owner_request("query"):          # not the path: nothing is marked
        assert not owner_requests.in_owner_request() and owner_requests.pending() == 0
        assert not (await run_db_read(where)).startswith("owner-request")


@pytest.mark.asyncio
async def test_the_switch_sends_owner_frames_back_to_the_default_pool(database, monkeypatch):
    monkeypatch.setenv(owner_requests.EXECUTOR_ENV, "off")
    with owner_requests.owner_request("truth_claim_cards"):
        name = await run_db_read(lambda conn: threading.current_thread().name)
    assert not name.startswith("owner-request")


@pytest.mark.asyncio
async def test_the_principal_and_the_mark_reach_the_owner_executor():
    from topos.principal import current_principal, reset_principal, set_principal

    token = set_principal(OWNER)
    try:
        with owner_requests.owner_request("signal_data_health"):
            seen = await owner_requests.run_off_loop(
                lambda: (current_principal(), owner_requests.in_owner_request(), threading.current_thread().name))
    finally:
        reset_principal(token)
    assert seen[0] == OWNER and seen[1] is True and seen[2].startswith("owner-request")


def test_the_count_is_lowered_however_the_frame_leaves():
    with pytest.raises(ValueError):
        with owner_requests.owner_request("calibration_catalog"):
            assert owner_requests.pending() == 1
            raise ValueError("as raised")
    assert owner_requests.pending() == 0


# -- 2. the device read ----------------------------------------------------------------------------------------------
# (its snapshot's single flight and awaited first walk: tests/topos/test_device_info_snapshot_single_flight.py)

@pytest.mark.asyncio
async def test_the_device_read_is_on_the_path_and_answers_while_a_fake_rebuild_holds_the_default_pool(monkeypatch, tmp_path):
    import topos.services.local as local_mod
    from topos.config.settings import settings

    db_path = tmp_path / "database.db"
    db_path.write_bytes(b"a" * 4096)
    walks: list[str] = []

    def slow_walk(_path):
        walks.append(threading.current_thread().name)
        time.sleep(0.2)
        return 4096, {"total_bytes": 4096, "sqlite_bytes": 4096, "raw_files_bytes": 0, "categories": []}

    monkeypatch.setattr(local_mod, "_storage_snapshot", local_mod.StorageSnapshot(slow_walk))
    monkeypatch.setattr(settings, "topos_database_mode", "sqlite")
    monkeypatch.setattr(local_mod, "_resolve_device_database_path", lambda: db_path)
    monkeypatch.setattr(state, "db_conn", None, raising=False)
    monkeypatch.setattr(state, "sync_client", None, raising=False)
    monkeypatch.setattr(state, "get_system_info", lambda: {"hostname": "test-host"}, raising=True)
    async with ParkedPool(workers=2):
        started = time.monotonic()
        responses = await asyncio.gather(*[asyncio.wait_for(handle_control_plane_request(
            {"id": f"req-d{n}", "type": "get_device_info", "payload": {}}, principal=OWNER), BUDGET_SECONDS)
            for n in range(20)])
        elapsed = time.monotonic() - started
    assert all(r.get("status") == "ok" and r["payload"]["database_size_bytes"] == 4096 for r in responses), responses[0]
    assert elapsed < BUDGET_SECONDS and walks == ["storage-snapshot"]
    assert owner_requests.handled() == 20 and owner_requests.pending() == 0


def _wait_until(condition, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


# -- 3. pacing ------------------------------------------------------------------------------------------------------

def test_pace_returns_at_once_with_nothing_pending():
    assert owner_requests.pace() == 0.0


def test_pace_waits_while_an_owner_frame_is_pending_and_returns_when_it_ends():
    ended = threading.Event()

    def frame():
        with owner_requests.owner_request("truth_claim_cards"):
            ended.wait(5)

    thread = threading.Thread(target=frame)
    thread.start()
    _wait_until(lambda: owner_requests.pending() == 1, 2.0)
    threading.Timer(0.15, ended.set).start()
    waited = owner_requests.pace(limit=5.0)
    thread.join(5)
    assert 0.1 <= waited < 2.0
    assert owner_requests.yields()["count"] == 1 and owner_requests.yields()["longest"] == pytest.approx(waited)


def test_pace_is_bounded_and_never_waits_under_the_gate_on_a_loop_or_with_the_switch_off(monkeypatch):
    with owner_requests.owner_request("calibration_catalog"):
        assert 0.05 <= owner_requests.pace(limit=0.05) < 1.0
        with with_db_write():
            assert owner_requests.pace(limit=1.0) == 0.0
        assert asyncio.run(_pace_on_a_loop()) == 0.0
        monkeypatch.setenv(owner_requests.PACING_ENV, "off")
        assert owner_requests.pace(limit=1.0) == 0.0


async def _pace_on_a_loop() -> float:
    return owner_requests.pace(limit=1.0)


def test_the_rebuild_queue_waits_between_shares_while_an_owner_frame_is_pending(tmp_path):
    """A fake slow rebuild queue of two shares: with an owner frame in hand no share builds; when it ends, both do."""
    from types import SimpleNamespace

    from topos.permissions_v2.index_rebuilds import IndexRebuilds

    built: list[str] = []

    class Service:
        def restamp(self, grant_id, *, now):
            return None

        def rebuild(self, grant_id, *, now):
            built.append(grant_id)
            return {"state": "ready", "member_count": 0}

    ledger = SimpleNamespace(identity=SimpleNamespace(owner_id="owner-1"))
    queue = IndexRebuilds(ledger=ledger, root=tmp_path, index=Service)
    try:
        with owner_requests.owner_request("signal_list_entities"):
            queue.request(["grant-a", "grant-b"])
            time.sleep(0.3)
            assert built == []
        assert queue.wait_idle(10)
        assert built == ["grant-a", "grant-b"]
        assert owner_requests.yields()["count"] >= 1
    finally:
        queue.close()


def test_the_real_share_index_build_consults_the_yield_point_on_the_ungated_snapshot_only(tmp_path, monkeypatch):
    """`_rebuild_once` paces between batches of its walk and before each passage embedding, never under the gate."""
    from topos.storage.db.write_gate import db_write_lock
    from tests.permissions_v2.direct_search_twins import build

    calls: list[bool] = []
    real_pace = owner_requests.pace

    def recording(**how):
        calls.append(bool(db_write_lock()._is_owned()))
        return real_pace(**how)

    monkeypatch.setattr(owner_requests, "pace", recording)
    node = build(tmp_path / "paced", members=12, hidden_facts=2, seed=3)   # a ready p2c-v3 node: one real build
    assert len(calls) >= 2, "the build never reached a yield point"
    assert not any(calls), "a yield point was consulted while the build held the write gate"
    calls.clear()
    assert node.rebuild()[node.search_raw["binding"]["grant_id"]] == "ready"
    assert len(calls) >= 2 and not any(calls)
