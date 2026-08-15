"""M1 escalation ladder + the logging boundary.

The privacy tests here are not documentation. PLAN_SCOPE_CLASSIFIER.md §6.4 rule 1 says
no user text and no embedding of user text leaves the node, "enforced at the telemetry
boundary, not by policy" — so the boundary gets a test that fails if anyone widens it.
"""

from __future__ import annotations

import json

import pytest

from topos.query.scope_classifier import SOURCE_LLM, SOURCE_PROTOTYPE, ScopeVerdict
from topos.query.scope_router import (
    OUTCOME_ABSTAINED,
    OUTCOME_ANSWERED,
    OUTCOME_HARD,
    EscalationRecord,
    JsonlEscalationLog,
    ScopeRouter,
    aggregate_telemetry,
)

SENTINEL = "zzqx-my-therapist-said-something-private"


def _verdict(labels=(), confidence=0.9, escalated=False, scores=None):
    return ScopeVerdict(
        tuple(labels), confidence, SOURCE_PROTOTYPE, escalated, scores or {}
    )


def _router(tmp_path, *, verdict, escalate=None, **kw):
    return ScopeRouter(
        escalate=escalate,
        log=JsonlEscalationLog(tmp_path / "esc.jsonl"),
        classify_fn=lambda text, **_: verdict,
        **kw,
    )


# --- ladder -----------------------------------------------------------------


def test_confident_answer_does_not_escalate(tmp_path) -> None:
    calls = []
    router = _router(
        tmp_path,
        verdict=_verdict(labels=("health:read",), scores={"health:read": 0.9}),
        escalate=lambda t: calls.append(t) or ["schedule:read"],
    )
    out = router.route("how did I sleep")
    assert out.labels == ("health:read",)
    assert calls == []
    assert router.stats.answered == 1


def test_in_band_escalates_and_returns_llm_labels(tmp_path) -> None:
    router = _router(
        tmp_path,
        verdict=_verdict(labels=(), confidence=0.35, escalated=True),
        escalate=lambda t: ["schedule:read"],
    )
    out = router.route("something ambiguous")
    assert out.labels == ("schedule:read",)
    assert out.source == SOURCE_LLM
    assert out.escalated is True
    assert router.stats.hard == 1


def test_below_floor_abstains_and_opens_nothing(tmp_path) -> None:
    calls = []
    router = _router(
        tmp_path,
        verdict=_verdict(labels=(), confidence=0.1, escalated=False),
        escalate=lambda t: calls.append(t) or ["health:read"],
    )
    out = router.route("what is the capital of Mongolia")
    assert out.labels == ()
    assert out.abstained is True
    assert calls == [], "an abstain must not reach the LLM"
    assert router.stats.abstained == 1


# --- fail closed ------------------------------------------------------------


def test_llm_failure_holds_the_abstain(tmp_path) -> None:
    """An LLM outage must never open a scope."""

    def _boom(_text):
        raise RuntimeError("provider down")

    router = _router(
        tmp_path, verdict=_verdict(confidence=0.35, escalated=True), escalate=_boom
    )
    out = router.route("ambiguous")
    assert out.labels == ()
    assert out.escalated is True
    assert router.stats.llm_failures == 1


def test_no_escalator_wired_holds_the_abstain(tmp_path) -> None:
    router = _router(tmp_path, verdict=_verdict(confidence=0.35, escalated=True))
    assert router.route("ambiguous").labels == ()


def test_llm_labels_outside_the_live_registry_are_dropped(tmp_path) -> None:
    router = _router(
        tmp_path,
        verdict=_verdict(confidence=0.35, escalated=True),
        escalate=lambda t: ["publicBio:read", "not_a_scope", "health:read"],
    )
    out = router.route("ambiguous")
    assert out.labels == ("health:read",), "legacy and unknown ids must be filtered"


# --- the telemetry boundary -------------------------------------------------


def test_telemetry_never_carries_user_text(tmp_path) -> None:
    router = _router(
        tmp_path,
        verdict=_verdict(labels=(), confidence=0.33, escalated=True),
        escalate=lambda t: ["health:read"],
    )
    router.route(SENTINEL)
    blob = json.dumps(router.telemetry())
    assert SENTINEL not in blob
    for fragment in SENTINEL.split("-"):
        if len(fragment) > 3:
            assert fragment not in blob


def test_record_telemetry_is_closed_set_only() -> None:
    """Every value is a number, bool, or a scope id — nothing free-form."""
    record = EscalationRecord(
        outcome=OUTCOME_HARD,
        confidence=0.41,
        source=SOURCE_PROTOTYPE,
        predicted=("health:read",),
        runner_up=("schedule:read",),
        latency_ms=3.2,
        tau_high=0.42,
        tau_low=0.28,
        text=SENTINEL,
        ts=1.0,
    )
    telemetry = record.as_telemetry()
    blob = json.dumps(telemetry)
    assert SENTINEL not in blob

    from topos.query.scope_classifier import live_scope_ids

    allowed_strings = set(live_scope_ids()) | {
        OUTCOME_HARD, OUTCOME_ANSWERED, OUTCOME_ABSTAINED,
        SOURCE_PROTOTYPE, SOURCE_LLM,
        "very_low", "low", "mid", "high", "very_high",
        "sub_5ms", "sub_25ms", "sub_100ms", "sub_1s", "slow",
    }
    for key, value in telemetry.items():
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, str):
                assert item in allowed_strings, f"{key} leaked a free-form string: {item!r}"
            else:
                assert isinstance(item, (int, float, bool)), f"{key} has type {type(item)}"


def test_local_row_keeps_the_text_that_telemetry_drops() -> None:
    """The asymmetry is the design: full fidelity on the node, counts off it."""
    record = EscalationRecord(
        outcome=OUTCOME_HARD, confidence=0.4, source=SOURCE_PROTOTYPE,
        predicted=(), runner_up=(), latency_ms=1.0, tau_high=0.42, tau_low=0.28,
        text=SENTINEL, ts=1.0,
    )
    assert record.as_local_row()["text"] == SENTINEL
    assert "text" not in record.as_telemetry()


def test_confidence_is_bucketed_not_raw_in_telemetry() -> None:
    record = EscalationRecord(
        outcome=OUTCOME_HARD, confidence=0.4137, source=SOURCE_PROTOTYPE,
        predicted=(), runner_up=(), latency_ms=1.0, tau_high=0.42, tau_low=0.28,
    )
    telemetry = record.as_telemetry()
    assert telemetry["confidence_bucket"] == "mid"
    assert 0.4137 not in telemetry.values()


# --- node-local log ---------------------------------------------------------


def test_only_the_seam_is_logged(tmp_path) -> None:
    """Answered turns are the boring majority; hard and abstained are the training seam."""
    log = JsonlEscalationLog(tmp_path / "esc.jsonl")
    answered = ScopeRouter(
        log=log, classify_fn=lambda t, **_: _verdict(labels=("health:read",))
    )
    answered.route("confident")
    assert log.read() == []

    hard = ScopeRouter(
        log=log,
        escalate=lambda t: ["health:read"],
        classify_fn=lambda t, **_: _verdict(confidence=0.35, escalated=True),
    )
    hard.route("ambiguous")
    rows = log.read()
    assert len(rows) == 1
    assert rows[0]["outcome"] == OUTCOME_HARD


def test_logging_failure_does_not_break_routing(tmp_path) -> None:
    broken = JsonlEscalationLog(tmp_path / "nope" / "esc.jsonl")
    broken.path = tmp_path  # a directory — writing must fail
    router = ScopeRouter(log=broken, classify_fn=lambda t, **_: _verdict(confidence=0.1))
    assert router.route("anything").abstained is True


def test_log_can_be_disabled(tmp_path) -> None:
    log = JsonlEscalationLog(tmp_path / "esc.jsonl")
    router = ScopeRouter(
        log=log, enable_log=False, classify_fn=lambda t, **_: _verdict(confidence=0.1)
    )
    router.route("anything")
    assert log.read() == []


# --- curriculum signal ------------------------------------------------------


def test_confusion_pairs_are_tracked_for_the_curriculum_loop(tmp_path) -> None:
    """§6.5g — the node reports which pairs it cannot separate, as counts."""
    router = _router(
        tmp_path,
        verdict=_verdict(
            labels=("availability:read",),
            scores={"availability:read": 0.61, "schedule:read": 0.60},
        ),
    )
    router.route("am I free Friday")
    router.route("am I free Friday")
    assert router.telemetry()["confusion"] == {"availability:read ~ schedule:read": 2}


def test_escalation_rate_is_reported(tmp_path) -> None:
    router = _router(
        tmp_path,
        verdict=_verdict(confidence=0.35, escalated=True),
        escalate=lambda t: ["health:read"],
    )
    for _ in range(4):
        router.route("ambiguous")
    assert router.telemetry()["escalation_rate"] == 1.0
    assert router.telemetry()["total"] == 4


def test_aggregate_telemetry_over_local_rows_drops_text(tmp_path) -> None:
    log = JsonlEscalationLog(tmp_path / "esc.jsonl")
    router = ScopeRouter(
        log=log,
        escalate=lambda t: ["health:read"],
        classify_fn=lambda t, **_: _verdict(confidence=0.35, escalated=True),
    )
    router.route(SENTINEL)
    rolled = aggregate_telemetry(log.read())
    assert rolled["total"] == 1
    assert SENTINEL not in json.dumps(rolled)
