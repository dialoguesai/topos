"""`topos-node start --headless` — one node, detached, file logs, the port checked before boot.

Spike P0.8: uvicorn binds only after the app's lifespan has opened the database, so a
port held by another program used to cost a full boot (DB open, housekeeping, upgrades)
before "[Errno 48] address already in use". The pre-boot check must exit 4 without
touching the database; a Topos node already answering is exit 75 so a supervisor can
tell the two apart; the launchd foreground variant waits instead of exiting (KeepAlive
would otherwise restart-loop every 10 s).
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from topos.cli import commands
from topos.cli.commands import main


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOPOS_ENV_FILE", raising=False)
    # setenv-then-delenv: delenv alone records nothing for an ABSENT variable, so a
    # value the code under test sets for real would leak into later tests.
    monkeypatch.setenv("TOPOS_KEY", "")
    monkeypatch.delenv("TOPOS_KEY")
    monkeypatch.setenv("TOPOS_UDS_PATH", str(tmp_path / ".topos" / "engine.sock"))
    # `start` sets these for real (the node reads them at import); delenv first so
    # monkeypatch puts the pre-test state back and nothing leaks into later tests.
    # setenv-then-delenv: delenv alone records nothing for an ABSENT variable, so a
    # value the code under test sets for real would leak into later tests.
    monkeypatch.setenv("TOPOS_SKIP_UPDATE_CHECK", "")
    monkeypatch.delenv("TOPOS_SKIP_UPDATE_CHECK")
    # setenv-then-delenv: delenv alone records nothing for an ABSENT variable, so a
    # value the code under test sets for real would leak into later tests.
    monkeypatch.setenv("TOPOS_LOG_FILE", "")
    monkeypatch.delenv("TOPOS_LOG_FILE")
    monkeypatch.setenv("TOPOS_SUPERVISOR", "")
    monkeypatch.delenv("TOPOS_SUPERVISOR")
    monkeypatch.setattr(commands, "_run_node", lambda **_kw: (_ for _ in ()).throw(AssertionError("boot must not start")))
    return tmp_path


@pytest.fixture
def held_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        yield srv.getsockname()[1]


def _no_db(home: Path) -> None:
    assert not list(home.rglob("*.db")), "the pre-boot check opened a database"
    assert not Path(os.environ["TOPOS_DATABASE_PATH"]).exists()


def test_headless_exits_4_when_the_port_is_held_by_another_program(home: Path, held_port: int, monkeypatch):
    monkeypatch.setattr(commands, "_spawn_detached", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")))
    result = CliRunner().invoke(main, ["start", "--headless", "--port", str(held_port)])
    assert result.exit_code == 4
    assert result.stderr.strip() == f"Error: port {held_port} is in use by another program"
    _no_db(home)


def test_headless_exits_75_when_a_topos_node_already_answers(home: Path, held_port: int, monkeypatch):
    monkeypatch.setattr(commands, "_probe_running_node", lambda host, port: {"name": "topos-node", "version": "1.5.9", "pid": 777})
    monkeypatch.setattr(commands, "_spawn_detached", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")))
    result = CliRunner().invoke(main, ["start", "--headless", "--port", str(held_port)])
    assert result.exit_code == 75
    assert result.stderr.strip() == f"Error: Topos is already running on port {held_port} (pid 777)"
    _no_db(home)


def test_headless_exits_75_when_the_socket_is_live(home: Path, monkeypatch):
    # AF_UNIX paths are capped at 104 bytes on macOS; pytest's tmp_path is longer, so the
    # socket (and only the socket) lives in a short-lived /tmp folder, like the suite's own
    # pinned home (tests/topos_home_pin.py).
    short = Path(tempfile.mkdtemp(prefix="l7s-", dir="/tmp"))
    sock_path = short / "engine.sock"
    monkeypatch.setenv("TOPOS_UDS_PATH", str(sock_path))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free:
        free.bind(("127.0.0.1", 0))
        free_port = free.getsockname()[1]
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    try:
        monkeypatch.setattr(commands, "_spawn_detached", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")))
        result = CliRunner().invoke(main, ["start", "--headless", "--port", str(free_port)])
    finally:
        srv.close()
        shutil.rmtree(short, ignore_errors=True)
    assert result.exit_code == 75
    assert result.stderr.strip() == f"Error: Topos is already running (socket {sock_path})"
    _no_db(home)


def test_headless_spawns_a_detached_node_with_the_fixed_flags_and_file_logs(home: Path, monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free:
        free.bind(("127.0.0.1", 0))
        free_port = free.getsockname()[1]
    env_path = home / ".topos" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("TOPOS_KEY=tpn_" + "c" * 40 + "\n")
    monkeypatch.setenv("TOPOS_KEY", "tpn_" + "d" * 40)
    spawned = {}

    def _fake_spawn(argv, env, log_path):
        spawned["argv"] = argv
        spawned["env"] = env
        spawned["log_path"] = log_path
        return 31337

    monkeypatch.setattr(commands, "_spawn_detached", _fake_spawn)
    monkeypatch.setattr(commands, "_node_argv", lambda: ["/fake/bin/topos-node"])
    result = CliRunner().invoke(main, ["start", "--headless", "--port", str(free_port)])
    assert result.exit_code == 0, result.output
    assert spawned["argv"] == ["/fake/bin/topos-node", "--no-tray", "--skip-update-check", "--port", str(free_port)]
    assert spawned["log_path"] == home / ".topos" / "logs" / "node.log"
    assert spawned["env"]["TOPOS_LOG_FILE"] == str(home / ".topos" / "logs" / "node.log")
    assert spawned["env"]["TOPOS_SUPERVISOR"] == "headless"
    # The shell's differing TOPOS_KEY is not handed to the child; the file is the truth.
    assert spawned["env"].get("TOPOS_KEY") == "tpn_" + "c" * 40
    assert "Ignoring TOPOS_KEY from the shell environment" in result.stderr
    assert "tpn_" + "d" * 40 not in result.output
    assert "tpn_" + "c" * 40 not in result.output
    assert "pid 31337" in result.stdout
    assert str(home / ".topos" / "logs" / "node.log") in result.stdout


def test_spawn_detached_starts_its_own_session_and_logs_to_file(home: Path, monkeypatch):
    captured = {}

    class _Proc:
        pid = 99

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(commands.subprocess, "Popen", _fake_popen)
    log_path = home / ".topos" / "logs" / "node.log"
    pid = commands._spawn_detached(["/x/topos-node", "--no-tray"], {"A": "1"}, log_path)
    assert pid == 99
    assert captured["start_new_session"] is True
    assert captured["stdin"] == commands.subprocess.DEVNULL
    assert captured["stderr"] == commands.subprocess.STDOUT
    assert captured["env"] == {"A": "1"}
    assert log_path.exists()
    assert oct(log_path.stat().st_mode & 0o777) == "0o600"
    assert oct(log_path.parent.stat().st_mode & 0o777) == "0o700"


def test_headless_foreground_waits_while_a_node_is_running_then_serves(home: Path, monkeypatch):
    verdicts = iter([(75, "Topos is already running on port 8676 (pid 1)"), None])
    monkeypatch.setattr(commands, "_single_node_preflight", lambda host, port: next(verdicts))
    waits: list[float] = []
    monkeypatch.setattr(commands, "_sleep", waits.append)
    served = {}

    def _fake_run_node(**kw):
        served.update(kw)

    monkeypatch.setattr(commands, "_run_node", _fake_run_node)
    result = CliRunner().invoke(main, ["start", "--headless-foreground", "--port", "8676"])
    assert result.exit_code == 0, result.output
    assert waits == [30]
    assert served["tray"] is False
    assert served["skip_update_check"] is True
    assert served["port"] == 8676
    assert os.environ["TOPOS_LOG_FILE"] == str(home / ".topos" / "logs" / "node.log")
    assert os.environ["TOPOS_SUPERVISOR"] == "service"


def test_headless_foreground_exits_4_for_a_foreign_listener(home: Path, monkeypatch):
    monkeypatch.setattr(commands, "_single_node_preflight", lambda host, port: (4, f"port {port} is in use by another program"))
    monkeypatch.setattr(commands, "_sleep", lambda s: (_ for _ in ()).throw(AssertionError("must not wait")))
    result = CliRunner().invoke(main, ["start", "--headless-foreground", "--port", "8676"])
    assert result.exit_code == 4
    assert result.stderr.strip() == "Error: port 8676 is in use by another program"
