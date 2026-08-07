"""first_run decides whether the UI says "downloading" or "preparing".

Getting it wrong is a lie in the copy: a node that reports first_run=False shows
"preparing its language models… should only take a moment" while it fetches 2.9GB.
"""

from __future__ import annotations

import os

import pytest

from topos.sanitization import prewarm


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setattr(prewarm, "_hf_cache_root", lambda: str(tmp_path))
    return tmp_path


def _write_blobs(cache_root, repo_id: str, names: list[str]) -> None:
    blobs = os.path.join(prewarm._repo_dir(str(cache_root), repo_id), "blobs")
    os.makedirs(blobs, exist_ok=True)
    for name in names:
        open(os.path.join(blobs, name), "w").close()


REPO = prewarm.PRIVACY_FILTER_REPO


def test_absent_repo_is_not_present(cache_root):
    assert prewarm._repo_present(REPO) is False


def test_started_but_empty_is_not_present(cache_root):
    """The first instant of a first download — the directory exists, nothing is in it."""
    _write_blobs(cache_root, REPO, [])
    assert prewarm._repo_present(REPO) is False


def test_interrupted_download_is_not_present(cache_root):
    """The case that made this a bug: a killed first run leaves .incomplete blobs
    behind, and directory-existence alone would call that done."""
    _write_blobs(cache_root, REPO, ["abc123.incomplete"])
    assert prewarm._repo_present(REPO) is False


def test_partially_resumed_download_is_not_present(cache_root):
    _write_blobs(cache_root, REPO, ["abc123", "def456.incomplete"])
    assert prewarm._repo_present(REPO) is False


def test_complete_download_is_present(cache_root):
    _write_blobs(cache_root, REPO, ["abc123", "def456"])
    assert prewarm._repo_present(REPO) is True


def test_no_cache_root_is_not_present(monkeypatch):
    monkeypatch.setattr(prewarm, "_hf_cache_root", lambda: None)
    assert prewarm._repo_present(REPO) is False
