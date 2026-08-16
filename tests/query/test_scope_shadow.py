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


def test_shadow_is_wired_into_the_query_path_and_safely(tmp_path, monkeypatch) -> None:
    """Owner decision 2026-08-15: shadow IS in the query path.

    This test replaces an earlier guard asserting the opposite. That guard was written
    when the hook was deferred for a release and recorded the reason as "the owner asked
    for shadow collection that ships nothing"; the owner has since asked for in-path
    collection explicitly, because gold-labelled real traffic is the only measurement
    that can promote this classifier. The guard now enforces the terms of that decision
    instead of the decision it replaced: present, off by default, unable to break a turn.
    """
    import inspect

    from topos.query import pipeline
    from topos.query import scope_shadow as ss

    cls = next(
        obj for _n, obj in vars(pipeline).items()
        if inspect.isclass(obj) and hasattr(obj, "execute")
        and obj.__module__ == pipeline.__name__
    )
    src = inspect.getsource(cls.execute)
    assert "shadow" in src.lower(), "the shadow hook is gone from the query path"

    # Off by default: an unset flag and no flag file must leave the path byte-identical.
    monkeypatch.delenv(ss.ENV_FLAG, raising=False)
    monkeypatch.setattr(ss, "FLAG_FILE", tmp_path / "absent")
    assert ss.enabled() is False
    assert ss.observe("anything", "health:read") is None

    # And it cannot raise into the caller even when the scorer is broken.
    (tmp_path / "on").touch()
    monkeypatch.setattr(ss, "FLAG_FILE", tmp_path / "on")
    monkeypatch.setattr(ss, "_breaker_faults", 0)

    def _boom(_text):
        raise RuntimeError("scorer exploded")

    assert ss.observe("q", "health:read", classify_fn=_boom, force=True) is None


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


def test_cold_scorer_skips_and_never_loads_from_the_request_path(tmp_path, monkeypatch) -> None:
    """The daemon-thread warm is GONE. It put a 265 MB load in flight next to the
    engine's MPS work and tripped a torch GIL/MPS deadlock that wedged the node twelve
    times on 2026-08-15. Cold now means skip; `warm()` at startup is the only loader."""
    import threading

    from topos.query import scope_classifier as sc
    from topos.query import scope_shadow as ss

    sc.reset_cache()
    monkeypatch.setattr(ss, "FLAG_FILE", tmp_path / "on")
    (tmp_path / "on").touch()
    before = threading.active_count()

    log = ss.ShadowLog(tmp_path / "log.jsonl")
    assert ss.observe("q", "health:read", log=log) is None
    assert threading.active_count() == before, "observation must spawn NO loader thread"
    assert log.read() == []
    sc.reset_cache()


def test_warm_is_a_noop_when_shadow_is_disarmed(tmp_path, monkeypatch) -> None:
    from topos.query import scope_shadow as ss

    monkeypatch.delenv(ss.ENV_FLAG, raising=False)
    monkeypatch.setattr(ss, "FLAG_FILE", tmp_path / "absent")
    assert ss.warm() is False


def test_breaker_disables_observation_after_repeated_faults(tmp_path, monkeypatch) -> None:
    """Observation is telemetry: it may cost a millisecond, never a turn."""
    from topos.query import scope_shadow as ss

    monkeypatch.setattr(ss, "FLAG_FILE", tmp_path / "on")
    (tmp_path / "on").touch()
    monkeypatch.setattr(ss, "_breaker_faults", 0)
    monkeypatch.setattr(ss, "_breaker_announced", False)

    def _boom(_text):
        raise RuntimeError("scorer exploded")

    log = ss.ShadowLog(tmp_path / "log.jsonl")
    for _ in range(ss.BREAKER_LIMIT):
        assert ss.observe("q", "health:read", log=log, classify_fn=_boom, force=True) is None
    assert ss._breaker_tripped() is True
    # Tripped: even a working scorer is no longer consulted on the normal path.
    assert ss.observe("q", "health:read", log=log) is None
