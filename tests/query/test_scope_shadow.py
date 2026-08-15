"""Shadow mode must be invisible, unbreakable, and text-free off the node.

The wiring point is inside `QueryPipeline.execute`, so a bug here is a bug in every
query. These tests exist because "it only logs" is exactly the kind of claim that stops
being true after one refactor.
"""

from __future__ import annotations

import json

import pytest

from topos.query.scope_classifier import SOURCE_PROTOTYPE, ScopeVerdict
from topos.query.scope_shadow import (
    ENV_FLAG,
    VERDICT_ABSTAIN,
    VERDICT_ESCALATE,
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_OVER,
    ShadowLog,
    ShadowRecord,
    compare,
    enabled,
    observe,
    summarize,
)

SENTINEL = "zzqx-what-did-my-therapist-say"


def _verdict(labels=(), confidence=0.8, escalated=False):
    return ScopeVerdict(tuple(labels), confidence, SOURCE_PROTOTYPE, escalated, {})


# --- off by default ---------------------------------------------------------


def test_disabled_unless_the_env_flag_is_set(monkeypatch, tmp_path) -> None:
    from topos.query import scope_shadow as _ss

    monkeypatch.delenv(ENV_FLAG, raising=False)
    # Pin the flag file away from the real ~/.topos — the operator may legitimately
    # have shadow armed on this machine, and tests must not read live state.
    monkeypatch.setattr(_ss, "FLAG_FILE", tmp_path / "off")
    assert enabled() is False
    assert observe("anything", "health:read") is None


def test_flag_accepts_the_usual_truthy_spellings(monkeypatch, tmp_path) -> None:
    from topos.query import scope_shadow as _ss

    monkeypatch.setattr(_ss, "FLAG_FILE", tmp_path / "off")
    for value in ("1", "true", "yes", "ON"):
        monkeypatch.setenv(ENV_FLAG, value)
        assert enabled() is True, value
    for value in ("0", "false", "", "no"):
        monkeypatch.setenv(ENV_FLAG, value)
        assert enabled() is False, value


def test_disabled_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    log = ShadowLog(tmp_path / "shadow.jsonl")
    observe("anything", "health:read", log=log)
    assert log.read() == []


# --- never raises -----------------------------------------------------------


def test_a_broken_classifier_never_escapes(tmp_path) -> None:
    """A telemetry feature that can break a user's query is worse than none."""

    def _boom(_text):
        raise RuntimeError("model exploded")

    assert observe("q", "health:read", log=ShadowLog(tmp_path / "s.jsonl"),
                   classify_fn=_boom, force=True) is None


def test_an_unwritable_log_never_escapes(tmp_path) -> None:
    log = ShadowLog(tmp_path)  # a directory — the write must fail
    assert observe("q", "health:read", log=log,
                   classify_fn=lambda t: _verdict(("health:read",)), force=True) is None


# --- verdicts ---------------------------------------------------------------


def test_verdict_taxonomy() -> None:
    assert compare(["health:read"], "health:read", escalated=False) == VERDICT_HIT
    assert compare(["health:read", "places:read"], "health:read", escalated=False) == VERDICT_OVER
    assert compare(["places:read"], "health:read", escalated=False) == VERDICT_MISS
    assert compare([], "health:read", escalated=False) == VERDICT_ABSTAIN
    assert compare([], "health:read", escalated=True) == VERDICT_ESCALATE
    # An escalation is an escalation even if labels came back with it.
    assert compare(["health:read"], "health:read", escalated=True) == VERDICT_ESCALATE


def test_observe_records_the_comparison(tmp_path) -> None:
    log = ShadowLog(tmp_path / "s.jsonl")
    record = observe(
        "how did I sleep", "health:read", log=log,
        classify_fn=lambda t: _verdict(("attention:read",), confidence=0.46), force=True,
    )
    assert record is not None
    assert record.verdict == VERDICT_MISS
    assert record.true_scope == "health:read"
    assert record.predicted == ("attention:read",)
    rows = log.read()
    assert len(rows) == 1 and rows[0]["true_scope"] == "health:read"


# --- the telemetry boundary -------------------------------------------------


def test_telemetry_carries_no_user_text() -> None:
    record = ShadowRecord(
        verdict=VERDICT_MISS, true_scope="health:read", predicted=("places:read",),
        confidence=0.44, latency_ms=9.1, text=SENTINEL, ts=1.0,
    )
    blob = json.dumps(record.as_telemetry())
    assert SENTINEL not in blob
    assert "text" not in record.as_telemetry()
    assert record.as_local_row()["text"] == SENTINEL


def test_summary_telemetry_is_counts_and_scope_ids_only(tmp_path) -> None:
    log = ShadowLog(tmp_path / "s.jsonl")
    for true_scope, predicted in (
        ("health:read", ("health:read",)),
        ("health:read", ("attention:read",)),
        ("schedule:read", ("availability:read",)),
    ):
        observe(SENTINEL, true_scope, log=log,
                classify_fn=lambda t, p=predicted: _verdict(p), force=True)

    report = summarize(log.read())
    assert report.total == 3
    assert report.by_verdict[VERDICT_HIT] == 1
    assert report.by_verdict[VERDICT_MISS] == 2
    blob = json.dumps(report.as_telemetry())
    assert SENTINEL not in blob
    # §6.5g: the node reports which pairs it cannot separate, as counts.
    assert report.as_telemetry()["confusion"]["schedule:read -> availability:read"] == 1


def test_hit_rate_is_reported() -> None:
    rows = [
        {"verdict": VERDICT_HIT, "true_scope": "health:read", "predicted": ["health:read"]},
        {"verdict": VERDICT_MISS, "true_scope": "health:read", "predicted": ["places:read"]},
    ]
    assert summarize(rows).accuracy() == pytest.approx(0.5)


def test_cold_cache_is_skipped_rather_than_loaded_inline(monkeypatch, tmp_path) -> None:
    """A first observation measured 10.5s loading the model inside the request path."""
    monkeypatch.setenv(ENV_FLAG, "1")
    from topos.query import scope_classifier

    scope_classifier.reset_cache()
    log = ShadowLog(tmp_path / "s.jsonl")
    assert observe("q", "health:read", log=log) is None
    assert log.read() == [], "a cold cache must not be warmed from the request path"


# --- the pipeline wiring ----------------------------------------------------


def test_shadow_mode_is_NOT_wired_into_the_product() -> None:
    """Shadow mode is deliberately not node code — inverted 2026-08-15.

    The hook once lived in `QueryPipelineOrchestrator.execute`. Two facts killed it: the
    deployed node runs a frozen `uv tool` snapshot, so the repo hook never executed on the
    real node anyway; and the owner asked for shadow collection that ships nothing. It is
    now `topos-eval/scripts/shadow_offline_report.py`, which reads the node DB read-only.

    This test is the guard. Re-adding the hook should be a decision, not a drive-by.
    """
    import inspect

    from topos.query import pipeline

    cls = next(
        obj for _n, obj in vars(pipeline).items()
        if inspect.isclass(obj) and hasattr(obj, "execute")
        and obj.__module__ == pipeline.__name__
    )
    src = inspect.getsource(cls.execute)
    assert "shadow" not in src.lower(), (
        "shadow mode is back in the query path — that is product code, and the offline "
        "reporter exists so it does not have to be"
    )


def test_the_shadow_library_still_works_standalone(tmp_path) -> None:
    """Unwired, not deleted. The offline reporter and any future patch both use it."""
    log = ShadowLog(tmp_path / "s.jsonl")
    record = observe(
        "how did I sleep", "health:read", log=log,
        classify_fn=lambda t: _verdict(("health:read",)), force=True,
    )
    assert record is not None and record.verdict == VERDICT_HIT
    assert len(log.read()) == 1


def test_flag_file_enables_shadow_without_env(tmp_path, monkeypatch) -> None:
    """The app-shell node inherits no shell env, so the file is the reachable switch."""
    from topos.query import scope_shadow as ss

    monkeypatch.delenv(ss.ENV_FLAG, raising=False)
    monkeypatch.setattr(ss, "FLAG_FILE", tmp_path / "scope_shadow.on")
    assert ss.enabled() is False
    (tmp_path / "scope_shadow.on").touch()
    assert ss.enabled() is True


def test_cold_head_warms_off_thread_then_observes(tmp_path, monkeypatch) -> None:
    """With a head installed, the old prototypes-only guard skipped EVERY observation
    forever (retrieval warms a different slot). Now: first call skips and warms on a
    daemon thread; once resident, observation proceeds with no inline load."""
    import time as _time

    from topos.query import scope_classifier as sc
    from topos.query import scope_shadow as ss

    class _Verdict:
        labels = ("health:read",)
        confidence = 0.9
        escalated = False
        scores = {}
        reason = ""

    sc.reset_cache()
    monkeypatch.setattr(sc, "load_head", None, raising=False)
    monkeypatch.setattr(
        "topos.query.scope_head.load_head", lambda *a, **k: object()
    )
    monkeypatch.setattr(ss, "FLAG_FILE", tmp_path / "on")
    (tmp_path / "on").touch()
    observe = ss.observe
    if getattr(observe, "_warming", None):
        observe._warming = None

    log = ss.ShadowLog(tmp_path / "log.jsonl")
    first = observe("q", "health:read", log=log, classify_fn=lambda t: _Verdict())
    assert first is None  # cold: skipped, warming in the background
    for _ in range(50):
        if sc._head_cached.cache_info().currsize:
            break
        _time.sleep(0.05)
    second = observe("q", "health:read", log=log, classify_fn=lambda t: _Verdict())
    assert second is not None and second.verdict == ss.VERDICT_HIT
    sc.reset_cache()
    observe._warming = None
