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


def test_disabled_unless_the_env_flag_is_set(monkeypatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert enabled() is False
    assert observe("anything", "health:read") is None


def test_flag_accepts_the_usual_truthy_spellings(monkeypatch) -> None:
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


def test_pipeline_calls_observe_and_ignores_its_result() -> None:
    """The hook must be fire-and-forget: no branch below it may read the return."""
    import inspect

    from topos.query import pipeline

    cls = next(
        obj for _n, obj in vars(pipeline).items()
        if inspect.isclass(obj) and hasattr(obj, "execute")
        and obj.__module__ == pipeline.__name__
    )
    src = inspect.getsource(cls.execute)
    assert "_shadow_observe(query_text, scope_id)" in src
    assert "= _shadow_observe" not in src, "the result must not feed any decision"
