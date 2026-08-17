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
