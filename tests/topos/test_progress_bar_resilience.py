"""A progress bar must never be able to fail the work it narrates.

Live regression (2026-08-09): the `backfill-attention-triage-redo` upgrade step
died six seconds into a boot — before a single source had been touched — with
``ValueError: I/O operation on closed file.`` The upgrade runner executes steps
in a daemon thread ~20s after startup, and on a node launched detached by the
macOS shell (``--app --no-tray``) stderr had been closed by then. The very first
``ProgressBar.__enter__`` display raised, the runner ledgered the step 'failed',
and the data repair never happened.
"""

from __future__ import annotations

import io

import pytest

from topos.enrichment.progress_bar import ProgressBar

pytestmark = pytest.mark.public


class ClosedStream(io.StringIO):
    """Stands in for a stderr that was closed out from under the process."""

    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    def isatty(self) -> bool:
        raise ValueError("I/O operation on closed file.")

    def write(self, s):  # noqa: ANN001
        self.writes += 1
        raise ValueError("I/O operation on closed file.")


def test_closed_stream_does_not_raise_through_the_context_manager():
    stream = ClosedStream()
    with ProgressBar(total=3, desc="Upgrade backfill", file=stream) as bar:
        bar.set_description("Upgrade backfill · imessage")
        bar.update(1)
        bar.update(1)
        bar.update(1)
    # The work still counted; only the display was lost.
    assert bar.n == 3


def test_a_broken_stream_is_only_written_to_once():
    """Give up after the first failure instead of raising on every tick."""
    stream = ClosedStream()
    bar = ProgressBar(total=100, desc="x", file=stream)
    for _ in range(50):
        bar.update(1)
    bar.close()
    assert stream.writes == 1, "should stop writing after the stream proves broken"


def test_working_stream_still_gets_output():
    stream = io.StringIO()
    with ProgressBar(total=2, desc="Upgrade backfill", file=stream) as bar:
        bar.update(1)
        bar.update(1)
    assert "Upgrade backfill" in stream.getvalue()


def test_upgrade_step_survives_a_closed_stderr(monkeypatch, tmp_path):
    """End to end: the executor completes even when the progress bar cannot draw."""
    import sqlite3

    from topos.storage.db.migrations import apply_all_migrations
    from topos.upgrades import runner as upgrade_runner

    conn = sqlite3.connect(str(tmp_path / "u.db"), check_same_thread=False)
    apply_all_migrations(conn)
    conn.execute(
        "INSERT INTO timeline (event_at, record_id, source_id) "
        "VALUES (datetime('now'), 'r1', 'imessage')"
    )
    conn.commit()

    monkeypatch.setattr("sys.stderr", ClosedStream())

    async def fake_core(**kwargs):
        return {"status": "ok"}

    monkeypatch.setattr("topos.api.enrichment._process_enrichment_core", fake_core)

    detail = upgrade_runner._exec_enrichment_reprocess(
        {
            "id": "backfill-attention-triage-redo",
            "kind": "enrichment_reprocess",
            "params": {"job_names": ["attention_triage"], "force_reprocess": True},
            "_runner_step_index": 0,
            "_runner_steps_total": 1,
        },
        conn,
    )
    assert detail["sources"]["imessage"] == "ok"
    conn.close()
