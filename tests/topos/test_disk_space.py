"""Refusing a download for space must be a positive finding, never a guess.

The asymmetry that shapes this module: refusing a pull that would have fitted is
an annoyance the owner can work around, while allowing one that fills the volume
risks the node's SQLite — `runtime_housekeeping`: "ENOSPC mid-write is how
databases corrupt". So the check refuses on evidence and defers on everything
else.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from topos.engine.disk_space import (
    DEFAULT_RESERVE_BYTES,
    SpaceVerdict,
    check_space_for,
    format_bytes,
    ollama_models_dir,
    space_check_applies,
)

GB = 1024**3


def test_a_pull_that_fits_with_room_to_spare_is_allowed():
    with patch("topos.engine.disk_space.free_bytes", return_value=50 * GB):
        assert check_space_for(2_000_000_000) is None


def test_a_pull_that_does_not_fit_is_refused_with_the_numbers():
    with patch("topos.engine.disk_space.free_bytes", return_value=500_000_000):
        verdict = check_space_for(2_000_000_000)

    assert isinstance(verdict, SpaceVerdict)
    assert verdict.shortfall_bytes > 0
    message = verdict.message("llama3.2:latest")
    assert "llama3.2:latest" in message
    assert "2.0 GB" in message  # what it needs
    assert "500 MB" in message  # what there is


def test_a_pull_that_fits_but_leaves_no_headroom_is_still_refused():
    """"Fits" is not the bar — the node keeps writing after the model lands."""
    with patch("topos.engine.disk_space.free_bytes", return_value=2_100_000_000):
        verdict = check_space_for(2_000_000_000, reserve_bytes=DEFAULT_RESERVE_BYTES)

    assert verdict is not None, (
        "a 2 GB model landing with 100 MB to spare leaves the node one enrichment "
        "batch from the ENOSPC that corrupts its database"
    )


def test_a_remote_ollama_is_not_our_disk_to_judge():
    """The node can point at another machine; our free space says nothing."""
    with patch("topos.engine.disk_space.free_bytes", return_value=0):
        assert check_space_for(2_000_000_000, base_url="http://gpu-box:11434") is None


@pytest.mark.parametrize(
    "base_url,local",
    [
        ("http://localhost:11434", True),
        ("http://127.0.0.1:11434", True),
        ("http://[::1]:11434", True),
        ("", True),
        (None, True),
        ("http://gpu-box:11434", False),
        ("https://ollama.internal.example.com", False),
        ("http://10.0.0.5:11434", False),
    ],
)
def test_locality_of_the_ollama_host(base_url, local):
    assert space_check_applies(base_url) is local


def test_an_unreadable_volume_is_not_a_full_one():
    """None means "did not check" — the same rule the pack resolver uses."""
    with patch("topos.engine.disk_space.free_bytes", return_value=None):
        assert check_space_for(2_000_000_000) is None


def test_an_unknown_size_never_refuses():
    """A size we were never told is not a size that does not fit."""
    with patch("topos.engine.disk_space.free_bytes", return_value=0):
        assert check_space_for(None) is None
        assert check_space_for(0) is None
        assert check_space_for("nonsense") is None


def test_the_models_dir_follows_an_owner_who_moved_it(monkeypatch):
    """Checking the home volume is wrong for someone with a second drive."""
    monkeypatch.setenv("OLLAMA_MODELS", "/Volumes/Big/models")
    assert ollama_models_dir() == Path("/Volumes/Big/models")

    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    assert ollama_models_dir().name == "models"


def test_sizes_read_the_way_a_file_manager_shows_them():
    assert format_bytes(2_000_000_000) == "2.0 GB"
    assert format_bytes(500_000_000) == "500 MB"
    assert format_bytes(0) == "0 bytes"
    assert format_bytes(None) == "0 bytes"
    assert format_bytes("nonsense") == "0 bytes"
