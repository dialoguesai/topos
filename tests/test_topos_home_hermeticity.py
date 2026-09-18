"""The default test lane must not touch anything under the owner's ``~/.topos``.

``test_owner_database_hermeticity.py`` covers the database. This covers the rest of
the directory: ``.env`` (identity, and since the dual-mint the owner key),
``engine.sock`` (the owner socket the running node serves), ``cp_stamp_key.pub``,
``ingestion/``, the active-profile marker and the logs.

The hazard this file was written against (2026-09-18, MERGE_REHEARSAL.md §2.8
items 4-5): ``tests/topos/test_engine_presence_messages.py`` starts the app with a
control-plane URL, so the lifespan's dual-mint runs ``ensure_owner_key``. No
conftest set ``TOPOS_ENV_FILE``, so that READ the owner's real ``~/.topos/.env`` and
appended a minted ``TOPOS_OWNER_KEY`` when none was there. The same lifespan bound
the owner socket at ``~/.topos/engine.sock``. The live-DB guard never fired,
because it watches ``sqlite3.connect`` and nothing else. And the minted key stayed
in ``os.environ`` and on the settings singleton, so every later test ran in owner
mode.

Every test here runs under a SCRATCH home. A hermeticity test that had to look at
the real ``~/.topos`` to prove itself would be the bug it is guarding against.
"""

from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path

import pytest

from tests import live_db_watch


def _tree(root: Path) -> dict[str, tuple[str, int]]:
    """Every path under ``root`` -> (content digest, mtime_ns). Sockets by kind."""
    out: dict[str, tuple[str, int]] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        rel = str(p.relative_to(root))
        if p.is_socket():
            out[rel] = ("<socket>", st.st_mtime_ns)
        elif p.is_file():
            out[rel] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_mtime_ns)
        else:
            out[rel] = ("<dir>", 0)
    return out


@pytest.fixture
def scratch_home(monkeypatch: pytest.MonkeyPatch):
    """A home directory that looks like a developer's with a node on it.

    Seeded with a ``.topos/.env`` that holds a TOPOS_KEY and NO owner key: exactly
    the file the dual-mint appends to. HOME and ``Path.home`` both point here, so a
    default that is not pinned lands in this tree instead of the real one, where
    the test can see it.

    Under /tmp, not tmp_path. macOS caps an AF_UNIX path at 104 bytes, and
    ``<tmp_path>/.topos/engine.sock`` is longer, so under tmp_path an unpinned
    owner socket fails to bind (silently, on the supervisor thread) and this
    fixture would miss exactly the write it exists to see.
    """
    import shutil
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="th-", dir="/tmp")).resolve()
    (home / ".topos").mkdir(parents=True)
    (home / ".topos" / ".env").write_text("TOPOS_KEY=tk_scratch_not_real\n")
    # Keep Hugging Face on the real cache: a scratch HOME is otherwise a cold
    # cache, and startup's scope-head warm downloads the head again (~100s).
    monkeypatch.setenv(
        "HF_HOME",
        os.environ.get("HF_HOME") or str(live_db_watch.REAL_HOME / ".cache" / "huggingface"),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    yield home
    shutil.rmtree(home, ignore_errors=True)


async def _tree_after_settling(root: Path, before: dict, seconds: float = 3.0) -> dict:
    """The tree once background startup threads had a chance to act.

    The owner socket is bound by a supervisor THREAD (``topos.uds``), usually
    after the lifespan has already returned, so an immediate snapshot misses it.
    Returns as soon as anything changed; waits the full ``seconds`` otherwise.
    """
    import asyncio
    import time

    deadline = time.monotonic() + seconds
    while True:
        now = _tree(root)
        if now != before or time.monotonic() >= deadline:
            return now
        await asyncio.sleep(0.05)


def _fresh_owner_state() -> None:
    """The state a developer's machine starts a test run in: no owner key anywhere.

    Deliberately NOT through monkeypatch. Monkeypatch would put the previous values
    back at teardown and hide the leak this file is also about; restoring owner
    state is the conftest's job, and ``test_owner_mode_did_not_leak_*`` below checks
    it did it.
    """
    from topos.config.settings import settings

    os.environ.pop("TOPOS_OWNER_KEY", None)
    settings.topos_owner_key = None


async def _run_lifespan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, with_cp: bool) -> None:
    from topos import app as app_module
    from topos.config.settings import settings
    from topos.testing.lifespan import LifespanManager

    class _FakeControlPlaneClient:
        def __init__(self, control_plane_url, api_key, handler, verify_ssl=True):
            del control_plane_url, api_key, handler, verify_ssl

        def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def send_message(self, message) -> None:
            del message

    monkeypatch.setattr(
        settings, "control_plane_url", "ws://example.test/ws/engine" if with_cp else None,
        raising=False,
    )
    monkeypatch.setattr(settings, "topos_database_path", str(tmp_path / "engine.db"))
    monkeypatch.setattr(app_module, "ControlPlaneClient", _FakeControlPlaneClient)
    async with LifespanManager(app_module.app):
        pass


def test_conftest_pins_every_topos_home_default_outside_any_home() -> None:
    """The pins exist, and none of them points into a home directory."""
    from tests import topos_home_pin

    real_topos = os.path.realpath(os.path.join(live_db_watch.REAL_HOME, ".topos"))
    pinned = topos_home_pin.pinned_paths()
    for env_name in topos_home_pin.PINNED_ENV:
        value = os.environ.get(env_name)
        assert value, f"{env_name} is not pinned"
        assert os.path.realpath(value) == os.path.realpath(pinned[env_name]), env_name
        assert not os.path.realpath(value).startswith(real_topos + os.sep), (env_name, value)

    from topos import owner_key, relay_stamp, uds
    from topos.storage.raw.file_store import active_ingestion_base

    assert owner_key._env_path() == Path(pinned["TOPOS_ENV_FILE"])
    assert uds.socket_path() == Path(pinned["TOPOS_UDS_PATH"])
    assert active_ingestion_base() == Path(pinned["TOPOS_INGESTION_BASE_PATH"])
    assert Path(os.path.expanduser(relay_stamp._PINNED_KEY_PATH)).is_relative_to(
        topos_home_pin.pinned_root()
    )


@pytest.mark.asyncio
async def test_presence_lifespan_with_a_control_plane_leaves_home_untouched(
    scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact hazard: control-plane URL set -> dual-mint -> ensure_owner_key.

    Before the fix this appended ``TOPOS_OWNER_KEY=`` to ``$HOME/.topos/.env`` and
    bound ``$HOME/.topos/engine.sock``.
    """
    _fresh_owner_state()
    before = _tree(scratch_home)
    await _run_lifespan(monkeypatch, tmp_path, with_cp=True)
    after = await _tree_after_settling(scratch_home, before)
    assert after == before, {
        "created": sorted(set(after) - set(before)),
        "changed": sorted(k for k in before if k in after and after[k] != before[k]),
        "removed": sorted(set(before) - set(after)),
    }
    assert "TOPOS_OWNER_KEY" not in (scratch_home / ".topos" / ".env").read_text()


@pytest.mark.asyncio
async def test_plain_lifespan_leaves_home_untouched(
    scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full lifespan start with no control plane touches nothing under HOME either."""
    before = _tree(scratch_home)
    await _run_lifespan(monkeypatch, tmp_path, with_cp=False)
    after = await _tree_after_settling(scratch_home, before)
    assert after == before, {
        "created": sorted(set(after) - set(before)),
        "changed": sorted(k for k in before if k in after and after[k] != before[k]),
    }


@pytest.mark.asyncio
async def test_owner_mode_leak_setup_mints_a_key(
    scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First half of a pair: arm owner mode the way the presence test does.

    The mint lands in the PINNED env file (so this also proves the pin is what
    ``ensure_owner_key`` reads), and the key is left on the process exactly as the
    lifespan leaves it. The next test checks the conftest took it back off.
    """
    from tests import topos_home_pin
    from topos.config.settings import settings

    _fresh_owner_state()
    await _run_lifespan(monkeypatch, tmp_path, with_cp=True)
    assert settings.topos_owner_key, "the dual-mint did not run; this pair proves nothing"
    assert os.environ.get("TOPOS_OWNER_KEY") == settings.topos_owner_key
    pinned_env = Path(topos_home_pin.pinned_paths()["TOPOS_ENV_FILE"])
    assert f"TOPOS_OWNER_KEY={settings.topos_owner_key}" in pinned_env.read_text()


def test_owner_mode_did_not_leak_out_of_the_previous_test() -> None:
    """Second half. Runs after the one above (file order); alone it passes trivially.

    Before the fix two ``tests/topos/test_ingestion_sources.py`` tests got 403
    ``owner_mode_required`` on the beta lineage because this was still set.
    """
    from topos.config.settings import settings

    assert not settings.topos_owner_key
    assert "TOPOS_OWNER_KEY" not in os.environ


# -- the widened guard: any file operation under ~/.topos, not only sqlite ---------


def test_the_file_guard_watches_the_real_topos_home() -> None:
    """Snapshotted from the real home at import, before any test can move HOME."""
    assert live_db_watch.file_guard_is_installed()
    real = os.path.realpath(os.path.join(os.path.expanduser("~"), ".topos"))
    # This test runs under the real HOME, so the two must agree.
    assert real in live_db_watch.watched_roots()


@pytest.mark.parametrize(
    "operation, kind",
    [
        (lambda root: (root / ".env").read_text(), "read"),
        (lambda root: (root / ".env").open("a").write("X=1\n"), "write"),
        (lambda root: os.open(root / "new", os.O_WRONLY | os.O_CREAT), "write"),
        (lambda root: os.open(root / ".env", os.O_RDONLY), "read"),
        (lambda root: (root / "sub").mkdir(), "write"),
        (lambda root: os.replace(root / ".env", root / "moved"), "write"),
        (lambda root: os.remove(root / ".env"), "write"),
        (lambda root: os.chmod(root / ".env", 0o600), "write"),
    ],
    ids=["read_text", "append", "os.open-create", "os.open-read", "mkdir", "replace", "remove", "chmod"],
)
def test_any_file_operation_under_owner_data_is_refused_and_recorded(
    tmp_path: Path, operation, kind: str
) -> None:
    """Against a throwaway root armed as owner data, never the real one."""
    root = tmp_path / "pretend-topos"
    root.mkdir()
    (root / ".env").write_text("TOPOS_KEY=tk_scratch_not_real\n")
    before = _tree(root)
    with live_db_watch.watching_root(root) as capture:
        with pytest.raises(PermissionError) as excinfo:
            operation(root)
        found = capture.captured()
    assert _tree(root) == before, "the refusal must land BEFORE the operation"
    assert live_db_watch.ALLOW_ENV in str(excinfo.value)
    assert len(found) == 1, found
    assert found[0].kind == kind
    assert found[0].path.startswith(os.path.realpath(root))
    assert __file__ in found[0].origin


def test_binding_a_unix_socket_under_owner_data_is_refused() -> None:
    """The owner socket: binding one where the running node serves its own.

    Under /tmp, not tmp_path: macOS caps an AF_UNIX path at 104 bytes and checks
    that before the audit event fires, so a long tmp_path proves nothing.
    """
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="th-", dir="/tmp")).resolve()
    try:
        with live_db_watch.watching_root(root) as capture:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                with pytest.raises(PermissionError):
                    s.bind(str(root / "e.sock"))
            finally:
                s.close()
            found = capture.captured()
        assert not (root / "e.sock").exists()
        assert [f.kind for f in found] == ["write"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_operations_outside_owner_data_are_not_recorded(tmp_path: Path) -> None:
    root = tmp_path / "pretend-topos"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere.txt"
    with live_db_watch.watching_root(root) as capture:
        elsewhere.write_text("fine")
        elsewhere.read_text()
        found = capture.captured()
    assert found == []


def test_the_opt_out_downgrades_the_file_guard_to_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same escape hatch as the database guard: record everything, refuse nothing."""
    root = tmp_path / "pretend-topos"
    root.mkdir()
    monkeypatch.setenv(live_db_watch.ALLOW_ENV, "1")
    with live_db_watch.watching_root(root) as capture:
        (root / "x").write_text("allowed")
        found = capture.captured()
    assert (root / "x").read_text() == "allowed"
    assert [f.refused for f in found] == [False]
