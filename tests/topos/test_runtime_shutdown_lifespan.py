"""An app run owns its shutdown; the process it ran in does not inherit it.

`app.shutdown_event` has to tell cooperative worker threads (fact_llm, Ollama)
to stop — they cannot receive KeyboardInterrupt. It used to do that by setting
one process-lifetime flag that only a later `app.startup_event` could clear, so
the signal outlived the run that raised it. Two consequences, one latent and one
that bit every day:

  * a second app run in the same process cleared the flag at startup and
    UN-STOPPED the first run's still-draining workers;
  * anything else in the process after an app run — a CLI command, the next
    test — read "shutting down" from a run that had already finished. That is
    what made 16 tests in tests/features/test_fact_extraction_llm.py fail after
    any test here ran an app lifespan, while passing on their own.

Generations make the scope explicit: ending a run retires ITS generation
(forever) and installs a fresh one for whatever comes next.
"""

from __future__ import annotations

import pytest

from topos.runtime_shutdown import current_generation, is_shutdown_requested, stop_checker
from topos.testing.lifespan import LifespanManager


@pytest.mark.asyncio
async def test_app_shutdown_retires_its_own_run_only(monkeypatch, tmp_path):
    monkeypatch.setenv("TOPOS_KEY", "test-key")
    monkeypatch.setenv("CONTROL_PLANE_URL", "")
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(tmp_path / "engine.db"))
    from topos.app import app

    async with LifespanManager(app):
        run = current_generation()
        assert is_shutdown_requested() is False, "a live app run is not shutting down"

    # The run's own workers are told to stop, and stay stopped.
    assert stop_checker(run)() is True
    assert run.reason == "app_shutdown"

    # The process is NOT shutting down: that run is over, and a finished run
    # does not speak for whatever comes next.
    assert is_shutdown_requested() is False
    assert current_generation() is not run


@pytest.mark.asyncio
async def test_a_second_run_does_not_revive_the_first(monkeypatch, tmp_path):
    monkeypatch.setenv("TOPOS_KEY", "test-key")
    monkeypatch.setenv("CONTROL_PLANE_URL", "")
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(tmp_path / "engine.db"))
    from topos.app import app

    async with LifespanManager(app):
        first = current_generation()
    async with LifespanManager(app):
        second = current_generation()
        assert second is not first
        assert stop_checker(second)() is False, "the new run starts clean"
        # The old run's workers must NOT be un-stopped by the new startup.
        assert stop_checker(first)() is True


def test_shutdown_cancel_passes_every_catch_all():
    """Between a stopped graph fill and the job that owns it sit handlers that
    catch Exception. As an ordinary exception the stop was swallowed there and
    the import recorded done with its signal lane never run; as a BaseException
    it reaches the owner."""
    import asyncio

    from topos.runtime_shutdown import ShutdownInterrupt

    assert issubclass(ShutdownInterrupt, BaseException)
    assert not issubclass(ShutdownInterrupt, Exception)
    # ...and not a cancellation: asyncio rebuilds those at every task boundary,
    # which cost the upgrade runner the name it catches the stop by.
    assert not issubclass(ShutdownInterrupt, asyncio.CancelledError)

    async def _raises():
        raise ShutdownInterrupt("stopped")

    with pytest.raises(ShutdownInterrupt):
        asyncio.run(_raises())


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGTERM") or __import__("os").name != "posix",
                    reason="POSIX signal dispositions")
def test_a_sigterm_with_only_the_default_behind_the_hook_still_kills():
    """With nothing but the default disposition behind it (no uvicorn handler),
    the hook used to swallow SIGTERM and the process ran on until a SIGKILL."""
    import signal
    import subprocess
    import sys

    code = (
        "import os, signal, time\n"
        "from topos.runtime_shutdown import install_shutdown_signal_hooks\n"
        "install_shutdown_signal_hooks()\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
        "time.sleep(10)\n"
        "print('survived')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert "survived" not in result.stdout
    assert result.returncode == -signal.SIGTERM, (result.returncode, result.stderr[-400:])
