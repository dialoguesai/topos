"""The shadow log is bounded.

Shadow mode's local row carries the raw query text on purpose — that is what
makes the observations a usable test set (PLAN_SCOPE_CLASSIFIER.md §6.5), and
`as_telemetry()` strips it before anything leaves the node. But an append-only
record of every query a person ever ran, with no cap, is the wrong default for
a product whose security page promises the data stays theirs. These pin the
bound, not the feature.
"""

from __future__ import annotations

import json

import pytest

from topos.query.scope_shadow import ShadowLog, ShadowRecord, max_log_bytes


def _record(text: str) -> ShadowRecord:
    return ShadowRecord(
        verdict="hit",
        true_scope="messages:read:summary",
        predicted=("messages:read:summary",),
        confidence=0.9,
        latency_ms=1.0,
        text=text,
    )


def test_the_log_rotates_instead_of_growing_forever(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_SCOPE_SHADOW_MAX_BYTES", "400")
    log = ShadowLog(tmp_path / "shadow.jsonl")
    for i in range(60):
        log.append(_record(f"query number {i} with some padding text to add bytes"))
    live = tmp_path / "shadow.jsonl"
    rolled = tmp_path / "shadow.jsonl.1"
    assert live.is_file() and rolled.is_file(), "one generation is kept beside the live file"
    assert live.stat().st_size < 4000, "the live file must not grow without bound"


def test_only_one_generation_is_kept(tmp_path, monkeypatch):
    """Two files bound raw text at 2x the cap; ten would just be ten times the history."""
    monkeypatch.setenv("TOPOS_SCOPE_SHADOW_MAX_BYTES", "300")
    log = ShadowLog(tmp_path / "shadow.jsonl")
    for i in range(120):
        log.append(_record(f"query {i} padded out so rotation fires repeatedly"))
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["shadow.jsonl", "shadow.jsonl.1"], files


def test_rotation_does_not_lose_the_most_recent_observations(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_SCOPE_SHADOW_MAX_BYTES", "400")
    log = ShadowLog(tmp_path / "shadow.jsonl")
    for i in range(60):
        log.append(_record(f"query number {i} with padding to force a rotation"))
    log.append(_record("the newest one"))
    rows = log.read()
    assert any("the newest one" in json.dumps(r) for r in rows)


def test_rotation_off_by_setting_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_SCOPE_SHADOW_MAX_BYTES", "0")
    log = ShadowLog(tmp_path / "shadow.jsonl")
    for i in range(50):
        log.append(_record(f"query {i} padded out to exceed any small cap"))
    assert not (tmp_path / "shadow.jsonl.1").exists()
    assert max_log_bytes() == 0


def test_a_log_that_cannot_rotate_never_breaks_the_query_path(tmp_path, monkeypatch):
    """Same rule observe() applies to everything else here: never raise."""
    monkeypatch.setenv("TOPOS_SCOPE_SHADOW_MAX_BYTES", "10")
    log = ShadowLog(tmp_path / "shadow.jsonl")
    log.append(_record("first"))

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("pathlib.Path.rename", _boom)
    log.append(_record("second"))  # must not raise


def test_a_bad_env_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("TOPOS_SCOPE_SHADOW_MAX_BYTES", "not-a-number")
    assert max_log_bytes() == 8 * 1024 * 1024
