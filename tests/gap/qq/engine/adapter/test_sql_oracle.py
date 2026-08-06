"""Unit tests for SqlOracle class-conditioned graded_relevance (browse + recency)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

# Same guard as the sibling QQ tests (test_pii_relevance_judge_parse,
# test_en_qq_privacy_probe_corpus): topos-eval lives in another repo and is not
# installed in the release environment. Without this the module fails to import
# at collection, which aborts the whole run — `just gate` cannot even reach the
# public lane, so a release is blocked by a test the gate never meant to run.
pytest.importorskip("topos_eval", reason="topos-eval (sibling repo) not on PYTHONPATH")

from topos_eval.protocols.target import Request  # noqa: E402

from sql_oracle import SqlOracle, _recency_on_topic


def _req(text: str = "What was I doing last week?") -> Request:
    return Request(text=text, scope="messages:read")


def _oracle(**register_kw) -> SqlOracle:
    o = SqlOracle()
    o.register("What was I doing last week?", needle_groups=[], **register_kw)
    return o


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def test_recency_dated_item_in_window_grades_1() -> None:
    o = _oracle(query_class="recency", temporal_days=7)
    item = {
        "text": "demo talk downtown",
        "created_at": (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # Freeze "now" via helper contract: graded_relevance uses datetime.now — so use a
    # freshly recent ISO date relative to real now instead.
    recent = datetime.now(timezone.utc) - timedelta(days=3)
    item["created_at"] = recent.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert o.graded_relevance(_req(), item) == 1


def test_recency_dated_item_outside_window_grades_0() -> None:
    o = _oracle(query_class="recency", temporal_days=7)
    old = datetime.now(timezone.utc) - timedelta(days=60)
    item = {
        "text": "ancient vacation photos",
        "created_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    assert o.graded_relevance(_req(), item) == 0


def test_recency_undated_off_topic_grades_0() -> None:
    o = _oracle(query_class="recency", temporal_days=7)
    assert o.graded_relevance(_req(), {"text": "hello there general kenobi"}) == 0


def test_recency_last_n_days_stat_inside_window_grades_1() -> None:
    o = _oracle(query_class="recency", temporal_days=30)
    item = {"summary_text": "Last 30 days: journal entries category mix: topos 42%"}
    assert o.graded_relevance(_req(), item) == 1


def test_recency_last_n_days_stat_too_wide_grades_0() -> None:
    o = _oracle(query_class="recency", temporal_days=7)
    # Expanded window is 14d; a 90-day rollup is outside.
    item = {"summary_text": "Last 90 days: everything forever"}
    assert o.graded_relevance(_req(), item) == 0


def test_recency_never_grades_2_from_window_alone() -> None:
    o = _oracle(query_class="recency", temporal_days=7)
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    item = {
        "text": "busy week",
        "created_at": recent.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    assert o.graded_relevance(_req(), item) == 1


def test_known_item_ignores_window_without_needle() -> None:
    o = SqlOracle()
    o.register(
        "Who is Matteo?",
        needle_groups=[["Matteo Iraggi"]],
        query_class="known_item",
        temporal_days=7,  # should be ignored for known_item
    )
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    item = {
        "text": "unrelated chatter",
        "created_at": recent.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    req = _req("Who is Matteo?")
    assert o.graded_relevance(req, item) == 0
    assert o.graded_relevance(req, {"text": "Matteo Iraggi called"}) == 2


def test_browse_surface_membership_still_grades_1() -> None:
    o = SqlOracle()
    o.register(
        "Where do I go?",
        needle_groups=[],
        query_class="browse",
        surface_sources=("canonical:location_events",),
    )
    item = {
        "retrieval_source": "canonical:location_events",
        "display_name": "Home",
    }
    assert o.graded_relevance(_req("Where do I go?"), item) == 1


def test_from_cases_plumbs_temporal_days() -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")

    def oracle_fn(_conn):
        return SimpleNamespace(needle_groups=[], ok=True, note="")

    case = SimpleNamespace(
        oracle=oracle_fn,
        query_text=lambda _c: "What was I doing this past month?",
        topic_terms=(),
        negative=False,
        query_class="recency",
        expected_sources=(),
        temporal_days=30,
    )
    o = SqlOracle.from_cases([case], conn)
    truth = o.truth_for(_req("What was I doing this past month?"))
    assert truth is not None
    assert truth.query_class == "recency"
    assert truth.temporal_days == 30


def test_recency_on_topic_helper_window_math() -> None:
    in_win = {
        "text": "x",
        "created_at": (NOW - timedelta(days=10)).strftime("%Y-%m-%d"),
    }
    out_win = {
        "text": "x",
        "created_at": (NOW - timedelta(days=20)).strftime("%Y-%m-%d"),
    }
    assert _recency_on_topic(in_win, temporal_days=7, now=NOW) is True  # 14d expand
    assert _recency_on_topic(out_win, temporal_days=7, now=NOW) is False


@pytest.mark.parametrize(
    "claimed,temporal,expect",
    [
        (7, 7, True),
        (14, 7, True),
        (15, 7, False),
        (30, 30, True),
    ],
)
def test_recency_claimed_window_bounds(claimed: int, temporal: int, expect: bool) -> None:
    item = {"summary_text": f"Last {claimed} days: mix"}
    assert _recency_on_topic(item, temporal_days=temporal, now=NOW) is expect
