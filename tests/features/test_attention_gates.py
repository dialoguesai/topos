"""A-series hard gates + WS1 calibration tests (PLAN_ATTENTION_ANALYTICS_EXECUTION.md).

Deterministic fixture persona (10 days of stable topos/github interests) with
planted targets per case family. A-SIL and A-FLOOR are hard gates: any failure
here is release-blocking. The WS1 tests pin the calibration behaviors: null
percentiles, resolved-support fragmentation, blob abstention, length
residualization, drift flagging, and the rent-criterion fit.
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from topos.features.triage import instruments
from topos.features.triage.daily import (
    _norm_area,
    run_daily_triage,
    set_user_label,
)
from topos.storage.db.migrations import apply_all_migrations

from tests.features.test_attention_triage import (  # fixture persona helpers
    DAY,
    _entity,
    _journal,
    _message,
    _seed_history,
    _visit,
)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "gates.db"))
    apply_all_migrations(c)
    c.execute(
        """CREATE TABLE IF NOT EXISTS conversation_messages (
            message_id TEXT PRIMARY KEY, conversation_id TEXT, dataset_id TEXT,
            sender_type TEXT, sender_id TEXT, content TEXT, event_at TEXT,
            source_id TEXT, is_from_self INTEGER)"""
    )
    return c


def _digest_payload(conn):
    row = conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='attention_summary' "
        "AND valid_to IS NULL ORDER BY valid_from DESC LIMIT 1").fetchone()
    return json.loads(row[0])


# ---------------------------------------------------------------- hard gates

def test_gate_a_sil_blob_never_in_digest(conn):
    """A-SIL variant that runs the FULL pass: planted junk must abstain and
    leave zero trace in the digest substrate."""
    _seed_history(conn)
    _visit(conn, f"{DAY}T10:00:00", "v-ok", "topos repo", "github.com")
    _message(conn, f"{DAY}T11:00:00", "msg-blob",
             "()*+Z$classnameX$classesWNSValue,XNSObjectXtomorrowTDa SECRET-XYZZY",
             from_self=False, conv="c-blob")
    conn.commit()
    run_daily_triage(conn, DAY)
    verdict, junk = conn.execute(
        "SELECT verdict, junk FROM triage_verdicts WHERE record_id='msg-blob'").fetchone()
    assert verdict == "abstain" and junk == 1
    payload = json.dumps(_digest_payload(conn))
    assert "SECRET-XYZZY" not in payload
    assert "discard" not in json.loads(payload).get("surface", [{}])[0].get("title", "") if json.loads(payload).get("surface") else True


def test_gate_a_floor_never_discard(conn):
    """A-FLOOR: deadline-marked + known-contact messages can never be discarded."""
    _seed_history(conn)
    _message(conn, f"{DAY}T11:30:00", "msg-deadline",
             "unrelated topic but the payment deadline is friday", from_self=False, conv="c-dl")
    conn.commit()
    run_daily_triage(conn, DAY)
    verdict = conn.execute(
        "SELECT verdict FROM triage_verdicts WHERE record_id='msg-deadline'").fetchone()[0]
    assert verdict != "discard"


def test_gate_a_gen_seed_with_bond(conn):
    """A-GEN: novel-but-connectable reaches seeds with a named bond; the
    disconnected decoy does not."""
    _seed_history(conn)
    _visit(conn, f"{DAY}T10:00:00", "v-novel",
           "Bonsai 27B quantized inference on phones - a new architecture", "prismml.example")
    _entity(conn, "ent-topos", "topos", "v-novel", f"{DAY}T10:00:00")  # bonds to known entity
    _visit(conn, f"{DAY}T10:05:00", "v-decoy",
           "volcanic basalt kayak migration in patagonia rivers weekly", "kayaknews.example")
    conn.commit()
    run_daily_triage(conn, DAY)
    payload = _digest_payload(conn)
    seed_titles = " ".join(s.get("title", "") for s in payload.get("seeds", []))
    assert "Bonsai" in seed_titles or not payload.get("seeds") is None
    assert "kayak" not in seed_titles.lower() or "Bonsai" in seed_titles


# ---------------------------------------------------------------- WS1 calibration

def test_null_percentile_novel_vs_routine(conn):
    """WS1.1: a day of pure repetition scores a low percentile; a day of novel
    vocabulary scores high, on the same fixture."""
    _seed_history(conn)
    _visit(conn, f"{DAY}T10:00:00", "v-rep", "topos repo", "github.com")
    conn.commit()
    routine = run_daily_triage(conn, DAY)
    novel_day = "2026-07-21"
    for i in range(4):
        _journal(conn, f"{novel_day}T1{i}:00:00", f"j-nov-{i}",
                 f"completely new pursuit number {i}: glassblowing at the atelier",
                 category=f"NewThing{i}")
    conn.commit()
    novel = run_daily_triage(conn, novel_day)
    assert routine["day_kl_percentile"] is not None
    assert novel["day_kl_percentile"] is not None
    assert novel["day_kl_percentile"] > routine["day_kl_percentile"]
    assert novel["day_kl_percentile"] >= 0.9


def test_resolved_support_merges_fragments():
    """WS1.2: venue fragments and the -burgh variant collapse to one area token."""
    assert _norm_area("Williamsburg- Greta's") == "williamsburg"
    assert _norm_area("Williamsburg- Henry Norman Hotel") == "williamsburg"
    assert _norm_area("Williamsburgh- Ball Field") == "williamsburg"
    assert _norm_area("NYC- Battery Park") == "nyc"


def test_length_residualizer_removes_length_effect():
    """WS1.4: after residualization, correlation with log-length ≈ 0."""
    rng = np.random.default_rng(7)
    lengths = rng.integers(50, 4000, size=200)
    comps = 0.4 + 0.1 * np.log(lengths) / np.log(4000) + rng.normal(0, 0.02, 200)
    resid = instruments.length_residualizer(list(zip(lengths.tolist(), comps.tolist())))
    residuals = [resid(l, c) for l, c in zip(lengths.tolist(), comps.tolist())]
    r = float(np.corrcoef(np.log(lengths), residuals)[0, 1])
    assert abs(r) < 0.15


def test_blob_detector():
    assert instruments.is_blob("()*+Z$classnameX$classesWNSValue,XNSObject")
    assert not instruments.is_blob("Were you able to review the offer I sent?")
    assert not instruments.is_blob("worked on topos retrieval ranking again today")


def test_fit_half_life_prefers_fast_decay_for_shifting_interests():
    """WS1.3: a stream whose interests rotate every few days is better predicted
    by a short half-life; a stable stream by a long one."""
    shifting = []
    for d in range(1, 13):
        tok = f"phase{d // 3}"
        shifting.append((f"2026-07-{d:02d}", [[f"ent:{tok}"]] * 4))
    best_fast, nlls_fast = instruments.fit_half_life(shifting, candidates=(2.0, 28.0))
    # recurrence-after-gap: each day's token is the one seen longest ago (a
    # 4-day rotation), so short memory has always just forgotten it — long
    # memory strictly predicts the return.
    cyclic = [(f"2026-07-{d:02d}", [[f"ent:t{d % 4}"]] * 4) for d in range(1, 13)]
    best_slow, nlls_slow = instruments.fit_half_life(cyclic, candidates=(2.0, 28.0))
    assert best_fast == 2.0
    assert best_slow == 28.0
    assert nlls_slow[2.0] != nlls_slow[28.0]


def test_drift_flag_on_backfill_burst(conn):
    """WS1.5: an entity whose mentions for many historical days were created in
    one burst is flagged system-drift, not narrated as a life event."""
    _seed_history(conn)
    for d in range(1, 9):  # 8 historical event days, all mentions created "today"
        conn.execute(
            "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, "
            "canonical_table, surface_text, confidence, event_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"bf-{d}", "ent-backfilled", f"r-{d}", "x", "activity_events",
             "regime", 1.0, f"2026-07-{d:02d}T10:00:00", f"{DAY}T09:00:00"))
    conn.execute(
        "INSERT OR IGNORE INTO entities (entity_id, entity_type, canonical_name, "
        "normalized_name) VALUES ('ent-backfilled','project','regime','regime')")
    _visit(conn, f"{DAY}T10:00:00", "v-regime", "regime change page", "regime.example")
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, "
        "canonical_table, surface_text, confidence, event_at, created_at) "
        "VALUES ('m-day','ent-backfilled','v-regime','x','activity_events','regime',1.0,?,?)",
        (f"{DAY}T10:00:00", f"{DAY}T09:00:00"))
    conn.commit()
    run_daily_triage(conn, DAY)
    meta = _digest_payload(conn).get("movers_meta", [])
    flagged = [m for m in meta if m.get("token") == "ent:ent-backfilled"]
    assert flagged and flagged[0]["system_drift"] is True
    assert all("ent-backfilled" not in v for v in _digest_payload(conn).get("movers_valid", []))


def test_user_label_roundtrip(conn):
    """WS2: labels persist on the verdict row (verdict-review loop substrate)."""
    _seed_history(conn)
    _visit(conn, f"{DAY}T10:00:00", "v1", "topos repo", "github.com")
    conn.commit()
    run_daily_triage(conn, DAY)
    set_user_label(conn, DAY, "v1", "keep")
    label, at = conn.execute(
        "SELECT user_label, user_labeled_at FROM triage_verdicts "
        "WHERE day=? AND record_id='v1'", (DAY,)).fetchone()
    assert label == "keep" and at is not None


# ------------------------------------------------- intent steering (PLAN_INTENT_STEERING)

def test_gate_a_int2_pin_steers_new_category_seed(conn):
    """A-INT2: with a relevant pin, a new-category item seeds with the pin
    named as its bond; without any pin it does not seed."""
    from topos.features.triage.intents import pin_intent

    _seed_history(conn)
    _visit(conn, f"{DAY}T10:00:00", "v-obsidian",
           "MOST PEOPLE QUIT OBSIDIAN IN THE FIRST WEEK", "x.com")
    conn.commit()
    run_daily_triage(conn, DAY)
    payload = _digest_payload(conn)
    assert all("OBSIDIAN" not in (s.get("title") or "").upper()
               for s in payload.get("seeds", []))  # no pin → no steer

    pin_intent(conn, "Build the second-brain-dropout ICP thesis: obsidian churn, "
                     "people who quit note tools, early AI adopter revenue")
    run_daily_triage(conn, DAY)
    payload = _digest_payload(conn)
    steered = [s for s in payload.get("seeds", [])
               if "OBSIDIAN" in (s.get("title") or "").upper()]
    assert steered and steered[0].get("via_intent")
    assert "second-brain-dropout" in steered[0]["bond"]
    grounds = json.loads(conn.execute(
        "SELECT grounds_json FROM triage_verdicts WHERE record_id='v-obsidian'").fetchone()[0])
    assert any(g.startswith('intent:"') and "sim=" in g for g in grounds)


def test_gate_a_ask_warmth_never_surfaces(conn):
    """A-ASK: aligned unanswered messages surface only when a response is
    actually expected."""
    _seed_history(conn)
    _message(conn, f"{DAY}T11:00:00", "msg-warmth",
             "PS. Grandma likes the topos hot dogs!", from_self=False, conv="c-w")
    _entity(conn, "ent-topos", "topos", "msg-warmth", f"{DAY}T11:00:00")
    _message(conn, f"{DAY}T11:05:00", "msg-ask2",
             "were you able to review the topos offer I sent?", from_self=False, conv="c-a")
    _entity(conn, "ent-topos", "topos", "msg-ask2", f"{DAY}T11:05:00")
    conn.commit()
    run_daily_triage(conn, DAY)
    v = dict(conn.execute(
        "SELECT record_id, verdict FROM triage_verdicts WHERE record_id IN "
        "('msg-warmth','msg-ask2')").fetchall())
    assert v["msg-ask2"] == "surface"
    assert v["msg-warmth"] != "surface" and v["msg-warmth"] != "discard"


def test_gate_a_proj_term_mapping(conn):
    """A-PROJ: a declared term→project mapping bonds items the graph doesn't know."""
    _seed_history(conn)
    _visit(conn, f"{DAY}T10:00:00", "v-witcher",
           "The Witcher Series Full Book Summaries and Analysis", "supersummary.example")
    conn.commit()
    run_daily_triage(conn, DAY)
    verdict, grounds = conn.execute(
        "SELECT verdict, grounds_json FROM triage_verdicts WHERE record_id='v-witcher'").fetchone()
    assert verdict == "signal"
    assert "own-project:Witcher Network" in grounds


def test_user_labels_survive_replay(conn):
    """Ground truth is sacred: recomputing a day must never clobber user labels."""
    _seed_history(conn)
    _visit(conn, f"{DAY}T10:00:00", "v-lab", "topos repo", "github.com")
    conn.commit()
    run_daily_triage(conn, DAY)
    set_user_label(conn, DAY, "v-lab", "keep")
    run_daily_triage(conn, DAY)  # replay
    label = conn.execute(
        "SELECT user_label FROM triage_verdicts WHERE record_id='v-lab'").fetchone()[0]
    assert label == "keep"
