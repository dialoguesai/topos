"""The owner's own word on a claim they typed (the home claim box, owner's decision of 5 Nov 2026): the mark is one
owner-only ``verified_claim`` object, the next ``verify_claim`` of that statement answers from it at once, a mark on
a related claim is self-lane evidence the no-model judge reads with the right polarity, and Calibrate's card labels
stay out of it (``verify_v2.LABELS_AS_EVIDENCE`` untouched).

Synthetic only: a migrated temp database with invented rows; never the live node.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.truth_lane_support import bind, build_node, connect
from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
from topos.query import truth_checks as store
from topos.query.verify import verify_claim
from topos.query.verify_modes import FUN_LEXICONS, SENSITIVE_LEXICONS

CLAIM = "I moved to Denver in 2021"
APP = "topos-react-app"


@pytest.fixture
def node(tmp_path, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("TOPOS_TRUTH_LLM", "off")
    path = build_node(tmp_path, tmp_path_factory.getbasetemp())
    bind(monkeypatch, path)
    conn = connect(path)
    yield SimpleNamespace(path=path, conn=conn)
    conn.close()


def _objects(conn):
    rows = conn.execute("SELECT object_key, payload_json, confidence, source_refs_json, valid_to, created_by "
                        "FROM signal_objects WHERE object_type='verified_claim'").fetchall()
    return [dict(zip(("key", "payload", "confidence", "refs", "valid_to", "by"), r)) for r in rows]


# --- the store ---------------------------------------------------------------------------------------------------------
def test_a_mark_is_one_owner_only_verified_claim_object_with_no_record(node):
    out = store.label_own_claim(node.conn, "  I moved to  Denver in 2021 ", "partly", "  only for the summer  ")
    assert out["ok"] is True and out["label"] == "partly" and out["note"] == "only for the summer" and out["labelled_at"]
    [obj] = _objects(node.conn)
    assert obj["key"].startswith(store.OWN_CLAIM_KEY_PREFIX) and obj["by"] == "owner" and obj["valid_to"] is None
    assert obj["confidence"] == 0.5 and json.loads(obj["refs"]) == []
    assert json.loads(obj["payload"]) == {"claim": CLAIM, "label": "partly", "note": "only for the summer",
                                          "speaker_kind": "owner", "source": "claim_box", "disclosure": "owner_only"}


def test_the_same_claim_in_another_spelling_replaces_the_mark(node):
    store.label_own_claim(node.conn, CLAIM, "true")
    store.label_own_claim(node.conn, "i moved to denver in 2021.", "false", "it was 2022")
    [obj] = _objects(node.conn)
    assert obj["confidence"] == 0.0 and json.loads(obj["payload"])["label"] == "false"
    assert store.own_claim_label(node.conn, "“I moved to Denver in 2021!”")["label"] == "false"
    assert store.own_claim_label(node.conn, "I moved to Denver in 2020") == {"label": None, "note": None, "labelled_at": None}


def test_a_mark_can_be_removed(node):
    store.label_own_claim(node.conn, CLAIM, "true")
    assert store.clear_own_claim_label(node.conn, CLAIM) == {"ok": True, "label": None, "note": None, "labelled_at": None}
    assert _objects(node.conn) == [] and store.own_claim_label(node.conn, CLAIM)["label"] is None


@pytest.mark.parametrize("statement, label, note", [
    (None, "true", None), ("", "true", None), ("...", "true", None), ("x" * 1001, "true", None),
    (CLAIM, "maybe", None), (CLAIM, None, None), (CLAIM, "true", 3),
])
def test_a_bad_mark_is_refused_with_a_fixed_code(node, statement, label, note):
    with pytest.raises(store.TruthRequestError, match="invalid_truth_label"):
        store.label_own_claim(node.conn, statement, label, note)
    assert _objects(node.conn) == []


def test_only_the_claim_box_marks_are_listed_never_a_cards_label(node):
    store.label_own_claim(node.conn, CLAIM, "true")
    now = store.now_utc().isoformat()
    payload = {"claim": "The owner painted the shed.", "label": "true", "note": None, "speaker_kind": "owner",
               "source": "journal", "check_id": "chk1", "disclosure": "owner_only"}
    with store.with_db_write():
        store._write_verified_claim(node.conn, "truth_check:chk1", payload, 1.0, [], now, now)
        store.commit_connection(node.conn)
    assert len(_objects(node.conn)) == 2
    assert [row["claim"] for row in store.own_claim_labels(node.conn)] == [CLAIM]


# --- verify_claim answers from the mark ---------------------------------------------------------------------------------
def _check(conn, statement, mode=None):
    kwargs = {"mode": mode} if mode else {}
    return verify_claim(conn, statement=statement, caller_app_id=APP, **kwargs)


@pytest.mark.parametrize("label, stance, confidence", [("true", "supports", 1.0), ("false", "contradicts", 1.0),
                                                         ("partly", "supports", 0.5)])
def test_the_exact_claim_answers_from_the_mark_with_no_retrieval_and_no_judge(node, label, stance, confidence):
    assert _check(node.conn, CLAIM)["abstained"] is True, "nothing in an empty node"
    store.label_own_claim(node.conn, CLAIM, label, "a note the answer never carries")
    out = _check(node.conn, "i moved to denver in 2021.")
    assert out["lanes"]["self"] == {"stance": stance, "confidence": confidence}
    assert out["lanes"]["attributed"] == {"stance": "no_evidence", "confidence": 0.0}
    assert out["lanes"]["ambient"] == {"stance": "no_evidence", "confidence": 0.0}
    assert out["abstained"] is False
    audit = out["_audit"]
    assert audit["judge"] == "owner_label" and audit["own_label"] == f"exact:{label}"
    assert audit["retrieval_touched"] is False
    assert "note the answer" not in json.dumps({k: v for k, v in out.items() if k != "_audit"})


@pytest.mark.parametrize("label, stance", [("true", "supports"), ("false", "contradicts")])
def test_a_related_claim_reads_the_mark_as_self_lane_evidence(node, label, stance):
    store.label_own_claim(node.conn, CLAIM, label)
    out = _check(node.conn, "I moved to Denver")
    assert out["_audit"]["own_label"] == "related:1" and out["_audit"]["judge"] == "heuristic"
    assert out["lanes"]["self"]["stance"] == stance
    assert 0 < out["lanes"]["self"]["confidence"] <= 0.55, "a related mark corroborates; only the exact one decides"


def test_an_unrelated_claim_sees_no_mark(node):
    store.label_own_claim(node.conn, CLAIM, "true")
    out = _check(node.conn, "My favourite colour is green")
    assert out["_audit"]["own_label"] == "none" and out["abstained"] is True


def test_in_fun_mode_the_aperture_still_runs_first(node):
    sensitive = next(iter(next(iter(SENSITIVE_LEXICONS.values()))))
    fun = next(iter(next(iter(FUN_LEXICONS.values()))))
    secret = f"I was {sensitive} last year"
    store.label_own_claim(node.conn, secret, "true")
    out = _check(node.conn, secret, mode="fun")
    assert out["abstained"] is True and out["_audit"]["reason"] == "out_of_aperture"
    assert "own_label" not in out["_audit"], "refused before the mark is read"
    light = f"my {fun} is the best"
    store.label_own_claim(node.conn, light, "true")
    assert _check(node.conn, light, mode="fun")["lanes"]["self"] == {"stance": "supports", "confidence": 1.0}


# --- the door -------------------------------------------------------------------------------------------------------
def _as_owner(payload):
    from topos.core.handlers import HANDLERS

    token = set_principal(Principal(OWNER_APP, "uds"))
    try:
        return asyncio.run(HANDLERS["truth_own_claim_label"]({"id": "r1", "type": "truth_own_claim_label", "payload": payload}))
    finally:
        reset_principal(token)


def test_the_owner_marks_reads_and_removes_through_the_door(node):
    marked = _as_owner({"statement": CLAIM, "label": "true", "note": "yes"})
    assert marked["status"] == "ok" and marked["payload"]["ok"] is True and marked["payload"]["label"] == "true"
    read = _as_owner({"statement": CLAIM})
    assert read["payload"]["label"] == "true" and read["payload"]["note"] == "yes" and read["payload"]["labelled_at"]
    assert _as_owner({"statement": CLAIM, "label": None})["payload"]["label"] is None
    assert _as_owner({"statement": CLAIM})["payload"] == {"label": None, "note": None, "labelled_at": None}
    assert _as_owner({"statement": CLAIM, "label": "sure"})["error"] == "invalid_truth_label"
    assert _as_owner("not a dict")["error"] == "invalid_truth_label"
