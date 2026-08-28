"""An upgrade step approved from the UI must actually run, and must not lie when it doesn't.

Two defects, found together on a live node approving the 1.3.34 entities
reprocess. The step reported success in **6 milliseconds** having done nothing:

    detail_json: {"sources": {"browser_events": "error: asyncio.run() cannot be
    called from a running event loop", ... 11 of 11 ...}}
    status: done

1. **The consent path can never run a reprocess.** Boot calls the runner on a
   worker thread (``topos-upgrade-stamp``), where ``asyncio.run`` is fine --
   that is the only path anyone tested. But both consent entry points, ``POST
   /v1/upgrade/consent`` and the ``consent_upgrade_step`` websocket handler, are
   ``async def``, so the executor's ``asyncio.run`` raises inside the running
   loop. Every ``consent: prompt`` reprocess approved through the UI has been a
   silent no-op, and those are precisely the expensive steps nobody re-runs.

2. **A step that failed on every source was ledgered ``done``.** The runner
   ledgers ``done`` whenever an executor returns without raising, and these
   executors catch per-source exceptions and record them as strings. ``done``
   advances the baseline, so the work is never retried and the status endpoint
   shows a clean upgrade.

Either alone is survivable. Together they mean the failure is both total and
invisible, which is why both are pinned here.
"""

import asyncio

import pytest

from topos.upgrades.runner import _fail_if_every_source_errored, _run_coro

pytestmark = pytest.mark.public


class TestRunCoroWorksFromBothCallers:
    """The runner has two kinds of caller and only one was ever exercised."""

    def test_off_the_loop_as_boot_calls_it(self):
        async def work():
            return "ok"

        assert _run_coro(work()) == "ok"

    def test_inside_a_running_loop_as_the_consent_endpoints_call_it(self):
        """The regression: this raised RuntimeError and cost a whole upgrade step."""

        async def work():
            return "ok"

        async def caller():
            return _run_coro(work())

        assert asyncio.run(caller()) == "ok"

    def test_an_exception_still_reaches_the_caller_from_inside_a_loop(self):
        """Errors must not be swallowed by the worker thread."""

        async def boom():
            raise ValueError("real failure")

        async def caller():
            return _run_coro(boom())

        with pytest.raises(ValueError, match="real failure"):
            asyncio.run(caller())

    def test_the_result_is_the_coroutines_value_not_a_future(self):
        async def work():
            await asyncio.sleep(0)
            return {"rows": 7}

        async def caller():
            return _run_coro(work())

        assert asyncio.run(caller()) == {"rows": 7}


class TestATotalFailureIsNotDone:
    """`done` advances the baseline, so it must mean the work happened."""

    def test_every_source_failing_raises(self):
        outcomes = {
            "browser_events": "error: asyncio.run() cannot be called from a running event loop",
            "imessage": "error: asyncio.run() cannot be called from a running event loop",
        }

        with pytest.raises(RuntimeError) as excinfo:
            _fail_if_every_source_errored("reextract-entities", outcomes)

        # The message has to carry the cause, or the ledger row is a dead end.
        assert "2/2" in str(excinfo.value)
        assert "running event loop" in str(excinfo.value)

    def test_a_partial_failure_is_still_done(self):
        """One dead source must not block an upgrade for every other source."""
        _fail_if_every_source_errored(
            "step", {"a": "ok", "b": "error: source is offline"}
        )

    def test_all_ok_is_done(self):
        _fail_if_every_source_errored("step", {"a": "ok", "b": "ok"})

    def test_no_sources_at_all_is_not_a_failure(self):
        """A node with no real sources has nothing to reprocess and that is fine."""
        _fail_if_every_source_errored("step", {})
