"""The storage snapshot behind ``get_device_info`` is single-flight and never on the caller (services/local.py).

On the owner's node (1.5.29, 28 Oct 2026) the control plane's 60 s ``get_device_info`` poll found the 60 s cache
expired every time and walked ``dbstat`` over the whole 2.85 GB file on every poll; the frame was answered from its
stale snapshot once a minute, and any owner frame landing in that window waited past the relay budget. Pinned here
with a fake slow walk: twenty parallel reads run exactly one walk and every one of them answers under a second; a
value past the TTL is answered at once while one refresh runs behind the caller; only the first read after a start
waits for a walk; a walk that fails keeps the last value; the TTL outlasts the poll.
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

import topos.services.local as local_mod
from topos.core import state


class SlowWalk:
    """A ``dbstat`` walk that takes ``seconds`` and counts how many run at once."""

    def __init__(self, seconds: float = 0.3):
        self.seconds = seconds
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.fail = False

    def __call__(self, path):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.seconds)
            if self.fail:
                raise RuntimeError("walk failed")
            return 4096 * self.calls, {"total_bytes": 4096 * self.calls, "sqlite_bytes": 4096 * self.calls,
                                       "raw_files_bytes": 0, "categories": []}
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def device(monkeypatch, tmp_path):
    """``LocalDeviceService`` over a snapshot whose walk is slow and counted, with an adjustable clock."""
    from topos.config.settings import settings

    db_path = tmp_path / "database.db"
    db_path.write_bytes(b"a" * 4096)
    walk = SlowWalk()
    clock = {"now": 1000.0}
    snapshot = local_mod.StorageSnapshot(walk, ttl_s=60.0, first_wait_s=5.0, clock=lambda: clock["now"])
    monkeypatch.setattr(local_mod, "_storage_snapshot", snapshot)
    monkeypatch.setattr(settings, "topos_database_mode", "sqlite")
    monkeypatch.setattr(local_mod, "_resolve_device_database_path", lambda: db_path)
    monkeypatch.setattr(state, "db_conn", None, raising=False)
    monkeypatch.setattr(state, "sync_client", None, raising=False)
    monkeypatch.setattr(state, "get_system_info", lambda: {"hostname": "test-host"}, raising=True)
    return local_mod.LocalDeviceService(), snapshot, walk, clock


def _wait_until(condition, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


@pytest.mark.asyncio
async def test_twenty_parallel_device_reads_run_one_walk_and_each_answers_under_a_second(device):
    service, snapshot, walk, _clock = device
    started = time.monotonic()
    answered: list[float] = []

    async def read():
        info = await service.get_device_info()
        answered.append(time.monotonic() - started)
        return info

    infos = await asyncio.gather(*[read() for _ in range(20)])
    assert walk.calls == 1 and walk.max_active == 1 and snapshot.walks == 1
    assert len(answered) == 20 and max(answered) < 1.0, max(answered)
    assert {info.database_size_bytes for info in infos} == {4096}
    assert all(info.storage_breakdown["total_bytes"] == 4096 for info in infos)


@pytest.mark.asyncio
async def test_a_value_past_the_ttl_is_answered_at_once_and_refreshed_behind_the_caller(device):
    service, snapshot, walk, clock = device
    await service.get_device_info()                  # the first read: waits for the first walk
    clock["now"] += 61.0                              # past the TTL, as the control plane's poll found it
    started = time.monotonic()
    infos = await asyncio.gather(*[service.get_device_info() for _ in range(20)])
    elapsed = time.monotonic() - started
    assert elapsed < walk.seconds, f"a caller waited for the walk: {elapsed:.2f}s"
    assert {info.database_size_bytes for info in infos} == {4096}      # the last value, at once
    assert snapshot.walks == 2 and walk.max_active == 1                 # one refresh behind them
    await asyncio.to_thread(_wait_until, lambda: not snapshot.in_flight, 5.0)
    assert walk.calls == 2
    assert (await service.get_device_info()).database_size_bytes == 8192   # the refreshed value, no new walk
    assert snapshot.walks == 2


@pytest.mark.asyncio
async def test_the_first_read_waits_for_the_walk_and_a_failed_walk_keeps_the_last_value(device):
    service, snapshot, walk, clock = device
    started = time.monotonic()
    info = await service.get_device_info()
    assert time.monotonic() - started >= walk.seconds * 0.9 and info.database_size_bytes == 4096
    walk.fail = True
    clock["now"] += 61.0
    assert (await service.get_device_info()).database_size_bytes == 4096
    await asyncio.to_thread(_wait_until, lambda: not snapshot.in_flight, 5.0)
    assert walk.calls == 2
    assert snapshot.get(Path(snapshot._value[1]))[0] == 4096


@pytest.mark.asyncio
async def test_a_first_walk_that_outlasts_the_wait_answers_without_numbers_and_the_walk_still_finishes(monkeypatch, tmp_path):
    walk = SlowWalk(seconds=0.5)
    snapshot = local_mod.StorageSnapshot(walk, ttl_s=60.0, first_wait_s=0.1)
    started = time.monotonic()
    assert await asyncio.to_thread(snapshot.get, tmp_path / "database.db") == (None, None)
    assert time.monotonic() - started < 0.45
    await asyncio.to_thread(_wait_until, lambda: not snapshot.in_flight, 5.0)
    assert snapshot.get(tmp_path / "database.db")[0] == 4096 and walk.calls == 1


def test_the_ttl_outlasts_the_control_planes_poll():
    """A TTL equal to the poll interval made every poll a walk; the poll is 60 s."""
    assert local_mod._STORAGE_SNAPSHOT_TTL_S >= 10 * 60.0


def test_the_walk_never_runs_on_the_loops_default_pool_or_the_caller(device):
    """The refresh thread is the snapshot's own: its name says so, and the caller's thread never computes."""
    service, snapshot, walk, _clock = device
    seen: dict[str, str] = {}
    real = walk.__call__

    def named(path):
        seen["thread"] = threading.current_thread().name
        return real(path)

    snapshot._compute = named
    size, _ = snapshot.get(Path("x.db"))
    assert size == 4096 and seen["thread"] == "storage-snapshot" and seen["thread"] != threading.current_thread().name
