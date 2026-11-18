"""The node connects to the control plane without waiting for the storage walk, and its device-info seed lands.

protects: on a restart to install 1.5.56 (17 Nov 2026) a node was off the control plane for 32 s, and 20 s of it was
``topos/app.py`` awaiting ``get_device_info`` for the snapshot seed before ``ControlPlaneClient.start()``. The first read
after a start waits for the ``dbstat`` walk of the whole database (``_STORAGE_SNAPSHOT_FIRST_WAIT_S``, 20 s, since
1.5.30), and a multi-GB file takes all of it. The seed was then dropped: the service answers a ``DeviceInfoResponse``
and the seed was kept only when it was a dict, so it never landed after it was added (1.3.55), and a ``get_device_info``
the app loop could not answer in 5 s got the empty stub instead.

This starts the real app with a recorder for the client and a storage walk that does not end until the test lets it.
No network: the one request the pin thread makes is answered by a recorder that cannot be reached.
"""
from __future__ import annotations

import threading
import time

import pytest

from tests.test_topos_home_hermeticity import _fresh_owner_state, scratch_home  # noqa: F401 (fixture)


@pytest.mark.asyncio
async def test_the_client_starts_while_the_storage_walk_runs_and_holds_the_seed(scratch_home, tmp_path, monkeypatch):  # noqa: F811
    import httpx

    import topos.services.local as local_mod
    from topos import app as app_module
    from topos import relay_stamp
    from topos.__version__ import __version__
    from topos.config.settings import settings
    from topos.testing.lifespan import LifespanManager

    events: list[tuple[str, object]] = []
    walk_began: list[float] = []
    release = threading.Event()

    def walk(_path):
        """A walk of a large database: it ends only when the test lets it."""
        walk_began.append(time.monotonic())
        release.wait(30)
        return 4096, {"total_bytes": 4096, "sqlite_bytes": 4096, "raw_files_bytes": 0, "categories": []}

    class Client:
        """Stands in for ControlPlaneClient: it records the seed and the start, and connects to nothing."""

        def __init__(self, control_plane_url, api_key, handler, verify_ssl=True):
            pass

        def start(self) -> None:
            events.append(("start", time.monotonic()))

        async def stop(self) -> None:
            pass

        async def send_message(self, message) -> None:
            pass

        def set_device_info_snapshot(self, payload) -> None:
            events.append(("seed", payload))

    def unreachable(url, **_options):
        raise ConnectionError("the control plane cannot be reached")

    _fresh_owner_state()
    snapshot = local_mod.StorageSnapshot(walk, first_wait_s=10.0)
    monkeypatch.setattr(local_mod, "_storage_snapshot", snapshot)
    monkeypatch.setattr(settings, "control_plane_url", "wss://cp.example/ws/engine", raising=False)
    monkeypatch.setattr(settings, "topos_database_path", str(tmp_path / "engine.db"))
    monkeypatch.setattr(app_module, "ControlPlaneClient", Client)
    monkeypatch.setattr(relay_stamp, "_PINNED_KEY_PATH", str(tmp_path / "pin" / "cp_stamp_key.pub"))
    monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY", raising=False)
    monkeypatch.setattr(httpx, "get", unreachable)                    # the pin thread's one kind of request

    try:
        async with LifespanManager(app_module.app):
            assert [name for name, _ in events] == ["seed", "start"], events
            (_, seed), (_, started_at) = events
            # The seed landed, as the dict the client keeps, with what a relayed answer carries but the sizes.
            assert isinstance(seed, dict)
            assert seed["engine_version"] == __version__
            assert seed["database_size_bytes"] is None and seed["storage_breakdown"] is None
            # The client started while the walk the seed began still ran, not after the first-read wait (10 s here).
            assert walk_began and snapshot.in_flight
            assert started_at - walk_began[0] < 2.0, f"the client waited {started_at - walk_began[0]:.1f}s for the walk"
    finally:
        release.set()
