"""A pull that did not happen must never read as a pull that did.

Two defects close here, and the first is the dangerous one because it is silent:

  * Ollama reports a failed download as an ``{"error": ...}`` frame INSIDE a 200
    response. Nothing in the pull path looked at that field, so the stream ended
    cleanly and the record was marked ``done`` — a model that was never written,
    reported as installed. The setup card then advanced to "pack seeded" and the
    owner's first chat 404'd.
  * Nothing checked free disk before starting a multi-gigabyte write. The node's
    SQLite shares that volume, and ``runtime_housekeeping`` already records what
    filling it costs: "ENOSPC mid-write is how databases corrupt."
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from topos.engine import ollama_pull
from topos.engine.backends.ollama import OllamaPullFailed, PullAborted
from topos.engine.disk_space import SpaceVerdict


@pytest.fixture(autouse=True)
def _clean_progress():
    ollama_pull.reset_progress()
    yield
    ollama_pull.reset_progress()


class _Adapter:
    """Streams the frames it is given, exactly as `pull_model` would."""

    _base_url = "http://localhost:11434"

    def __init__(self, frames):
        self._frames = frames

    def pull_model(self, tag, *, stream=True, on_progress=None):
        for frame in self._frames:
            failure = str(frame.get("error") or "").strip()
            if failure:
                raise OllamaPullFailed(failure)
            if on_progress is not None:
                on_progress(frame)


def _roomy():
    return patch("topos.engine.ollama_pull.check_space_for", return_value=None)


def test_an_error_frame_mid_stream_is_a_failure_not_a_completion():
    """The silent one: a 200 response whose stream carries an error."""
    adapter = _Adapter(
        [
            {"status": "pulling manifest"},
            {"status": "downloading", "completed": 100, "total": 2_000_000_000},
            {"error": "write /root/.ollama/blobs: no space left on device"},
        ]
    )

    with _roomy():
        ollama_pull._run_pull("llama3.2:latest", adapter)

    record = ollama_pull.pull_status("llama3.2:latest")
    assert record["state"] == ollama_pull.STATE_ERROR, (
        "a failed download was recorded as complete — the card would advance and "
        "the first chat would 404 on a model that was never written"
    )
    assert "no space left on device" in record["error"]


def test_a_clean_stream_still_completes():
    adapter = _Adapter(
        [
            {"status": "downloading", "completed": 2_000_000_000, "total": 2_000_000_000},
            {"status": "success"},
        ]
    )

    with _roomy():
        ollama_pull._run_pull("llama3.2:latest", adapter)

    assert ollama_pull.pull_status("llama3.2:latest")["state"] == ollama_pull.STATE_DONE


def test_a_known_size_is_refused_before_a_single_byte_moves():
    verdict = SpaceVerdict(
        needed_bytes=2_000_000_000,
        free_bytes=100_000_000,
        reserve_bytes=2 * 1024**3,
        path="/home/x/.ollama/models",
    )
    started = []

    class _NeverCalled(_Adapter):
        def pull_model(self, *a, **k):  # pragma: no cover - must not run
            started.append(1)

    with patch("topos.engine.ollama_pull.check_space_for", return_value=verdict):
        record = ollama_pull.start_pull(
            "llama3.2:latest",
            adapter=_NeverCalled([]),
            known_size_bytes=2_000_000_000,
        )

    assert record["state"] == ollama_pull.STATE_ERROR
    assert record["reason"] == ollama_pull.REASON_NO_SPACE
    assert started == [], "the transfer started despite there being no room for it"
    assert "Not enough disk space" in record["error"]


def test_the_stream_stops_as_soon_as_the_real_size_says_it_cannot_fit():
    """The backstop for a tag whose size we could not know up front.

    Seconds in, not at 97% — and before the write that would fill the volume the
    node's database lives on.
    """
    verdict = SpaceVerdict(
        needed_bytes=40_000_000_000,
        free_bytes=1_000_000_000,
        reserve_bytes=2 * 1024**3,
        path="/home/x/.ollama/models",
    )
    delivered = []

    class _Streaming(_Adapter):
        def pull_model(self, tag, *, stream=True, on_progress=None):
            for frame in self._frames:
                delivered.append(frame)
                on_progress(frame)

    adapter = _Streaming(
        [
            {"status": "downloading", "completed": 1, "total": 40_000_000_000},
            {"status": "downloading", "completed": 2, "total": 40_000_000_000},
        ]
    )

    with patch("topos.engine.ollama_pull.check_space_for", return_value=verdict):
        with pytest.raises(PullAborted):
            adapter.pull_model("huge:latest", on_progress=lambda f: ollama_pull._apply_frame("huge:latest", f))

    assert len(delivered) == 1, "the transfer kept going after it was told to stop"
    record = ollama_pull.pull_status("huge:latest")
    assert record["state"] == ollama_pull.STATE_ERROR
    assert record["reason"] == ollama_pull.REASON_NO_SPACE


def test_run_pull_keeps_the_structured_reason_when_it_aborts():
    """`_run_pull`'s generic handler must not overwrite the disk verdict."""
    verdict = SpaceVerdict(
        needed_bytes=40_000_000_000,
        free_bytes=1_000_000_000,
        reserve_bytes=2 * 1024**3,
        path="/home/x/.ollama/models",
    )

    class _Streaming(_Adapter):
        def pull_model(self, tag, *, stream=True, on_progress=None):
            on_progress({"status": "downloading", "completed": 1, "total": 40_000_000_000})

    with patch("topos.engine.ollama_pull.check_space_for", return_value=verdict):
        ollama_pull._run_pull("huge:latest", _Streaming([]))

    record = ollama_pull.pull_status("huge:latest")
    assert record["reason"] == ollama_pull.REASON_NO_SPACE
    assert "Not enough disk space" in record["error"]
