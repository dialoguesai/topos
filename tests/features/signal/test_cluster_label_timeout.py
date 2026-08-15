"""The labeler's timeout budget: a cold model is not a down model."""
from __future__ import annotations
import time
import pytest
from concurrent.futures import TimeoutError as FuturesTimeoutError
from topos.features.signal.cluster_labels import apply_llm_cluster_labels, warmup_timeout_sec

def _clusters(n=4):
    return [{"cluster_id": f"tc_{i}", "label": "term / label", "dimension": "memory",
             "primary_dimension": "memory", "member_count": 10 - i, "label_terms": [],
             "members": [{"text_preview": f"kayaking river paddling {i}", "metadata": {}}] * 3,
             "metadata": {}} for i in range(n)]

def test_first_call_gets_the_warmup_budget_not_the_ordinary_one():
    """A cold first call is what aborted a live 163-cluster pass at 10s."""
    seen = {}
    def _slow(prompt):
        if not seen:
            seen["first"] = True
            time.sleep(0.4)          # longer than the ordinary budget below
        return "River Kayaking" if len(seen) < 2 else "Mortgage Refinance"
    clusters = _clusters(2)
    n = apply_llm_cluster_labels(clusters, complete=_slow, mode="on",
                                 timeout_sec=0.15)   # ordinary budget < first call
    assert n == 2, "the warm-up budget must cover the first, slowest call"

def test_a_single_slow_cluster_does_not_kill_the_pass():
    calls = {"n": 0}
    def _one_bad(prompt):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("transient")
        return f"Cluster Name {calls['n']}"
    clusters = _clusters(4)
    stats = {}
    n = apply_llm_cluster_labels(clusters, complete=_one_bad, mode="on", stats=stats)
    assert n == 3, "one failure must cost one cluster, not the remainder"
    assert stats["aborted"] is False

def test_a_model_that_never_answers_still_costs_one_call():
    calls = []
    def _boom(prompt):
        calls.append(1)
        raise RuntimeError("ollama down")
    stats = {}
    assert apply_llm_cluster_labels(_clusters(5), complete=_boom, mode="on", stats=stats) == 0
    assert len(calls) == 1, "never answered => stop immediately"
    assert stats["aborted"] is True and "ollama down" in stats["aborted_reason"]

def test_repeated_failures_after_a_success_stop_the_pass():
    calls = {"n": 0}
    def _dies(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return "First Good Name"
        raise RuntimeError("died")
    stats = {}
    n = apply_llm_cluster_labels(_clusters(10), complete=_dies, mode="on", stats=stats)
    assert n == 1
    assert calls["n"] == 4, "one success then 3 consecutive failures => abort"
    assert stats["aborted"] is True

def test_an_aborted_pass_is_not_reported_as_a_completed_one(tmp_path):
    import sqlite3
    from topos.features.signal.topic_clustering import relabel_existing_clusters
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE topic_clusters (cluster_id TEXT PRIMARY KEY, label TEXT, dimension TEXT,
            member_count INTEGER, source_mix_json TEXT DEFAULT '{}', label_terms_json TEXT DEFAULT '[]',
            centroid_preview TEXT, metadata_json TEXT DEFAULT '{}', updated_at TEXT);
        CREATE TABLE topic_cluster_members (member_id TEXT PRIMARY KEY, cluster_id TEXT, record_id TEXT,
            source_id TEXT, record_type TEXT, text_preview TEXT, weight REAL DEFAULT 1.0,
            metadata_json TEXT DEFAULT '{}');
    """)
    conn.execute("INSERT INTO topic_clusters (cluster_id,label,dimension,member_count) VALUES"
                 " ('tc_a','term / label','memory',5)")
    conn.execute("INSERT INTO topic_cluster_members (member_id,cluster_id,record_id,source_id,"
                 "record_type,text_preview) VALUES ('m1','tc_a','r1','s','t','kayaking river')")
    conn.commit()
    def _boom(prompt):
        raise RuntimeError("ollama down")
    out = relabel_existing_clusters(conn, complete=_boom, mode="on")
    assert out["status"] == "aborted", "a no-op pass must not read as success"
    assert out["relabeled"] == 0 and "ollama down" in out["reason"]
    conn.close()

def test_warmup_budget_is_configurable(monkeypatch):
    monkeypatch.setenv("TOPOS_CLUSTER_LABEL_WARMUP_TIMEOUT", "5")
    assert warmup_timeout_sec() == 5.0
    monkeypatch.setenv("TOPOS_CLUSTER_LABEL_WARMUP_TIMEOUT", "nonsense")
    assert warmup_timeout_sec() == 90.0
