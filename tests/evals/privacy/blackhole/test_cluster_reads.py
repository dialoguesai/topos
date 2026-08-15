"""Cluster READ paths honour the black hole.

The producer refuses to mint a protected name and the rebuild withdraws the
ones written before protection — but both are guarantees about what THIS build
writes. A row that predates the rebuild, arrives by restore, or is written by
an older node against the same database still carries the name, and until now
every cluster read served it:

  * `packet["topic_clusters"]` was assembled from `load_topic_clusters_for_query`
    and attached with no filter, so a grantee or MCP query got the cluster row
    and its member excerpts whatever the summary policy did;
  * `signal_list_topic_clusters` / `..._members` took no guard at all, unlike
    every entity read, where `guard` is a required kwarg precisely so a
    forgotten call site raises instead of leaking.

These pin the read side. Withholding here is per-caller: the owner keeps their
own clusters, everyone else loses them.
"""

from __future__ import annotations

import pytest

from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
from topos.query.retrieval import _blackhole_policy_for_clusters, _cluster_text_blob
from tests.evals.privacy.blackhole.corpus import (
    BH_CANONICAL,
    OK_CANONICAL,
    build_blackhole_corpus,
)

pytestmark = [pytest.mark.bhlr, pytest.mark.private]


@pytest.fixture()
def corpus(tmp_path):
    c = build_blackhole_corpus(str(tmp_path / "cluster_reads.db"))
    yield c
    c.conn.close()


def _clusters() -> list[dict]:
    return [
        {"cluster_id": "tc-bh", "label": f"Dinners with {BH_CANONICAL}",
         "centroid_preview": "table for four", "label_terms": ["dinner"], "member_count": 6},
        {"cluster_id": "tc-ok", "label": f"{OK_CANONICAL} standup",
         "centroid_preview": "sprint planning", "label_terms": ["standup"], "member_count": 4},
    ]


# ------------------------------------------------- the retrieval packet


def test_grantee_loses_a_cluster_named_after_a_protected_entity(corpus):
    kept = _blackhole_policy_for_clusters(
        _clusters(), conn=corpus.conn, disclosure_tier="shared_summary"
    )
    assert [c["cluster_id"] for c in kept] == ["tc-ok"]


def test_owner_keeps_it_and_it_is_stamped_for_the_taint_feed(corpus):
    kept = _blackhole_policy_for_clusters(
        _clusters(), conn=corpus.conn, disclosure_tier="owner_raw"
    )
    assert [c["cluster_id"] for c in kept] == ["tc-bh", "tc-ok"]
    protected = next(c for c in kept if c["cluster_id"] == "tc-bh")
    assert protected["blackhole_protected"] is True
    assert "blackhole_protected" not in kept[1], "the control must not be stamped"


def test_the_name_is_caught_in_the_centroid_preview_not_just_the_label(corpus):
    clusters = [{"cluster_id": "tc-x", "label": "Sunday Dinners",
                 "centroid_preview": f"catching up with {BH_CANONICAL}", "label_terms": []}]
    assert _blackhole_policy_for_clusters(
        clusters, conn=corpus.conn, disclosure_tier="shared_summary"
    ) == []


def test_the_name_is_caught_in_label_terms(corpus):
    """label_terms are lifted verbatim from member text — a label-only scan misses them."""
    clusters = [{"cluster_id": "tc-x", "label": "Sunday Dinners",
                 "centroid_preview": "table for four", "label_terms": ["dinner", BH_CANONICAL]}]
    assert _blackhole_policy_for_clusters(
        clusters, conn=corpus.conn, disclosure_tier="shared_summary"
    ) == []
    assert BH_CANONICAL.lower() in _cluster_text_blob(clusters[0])


def test_an_unreadable_store_fails_closed_for_a_grantee_and_open_for_the_owner(tmp_path):
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "empty.db"))  # no blackhole tables at all
    conn.execute("CREATE TABLE entity_blackholes (bad INTEGER)")  # wrong shape → raises
    conn.commit()
    with pytest.raises(Exception):
        _blackhole_policy_for_clusters(_clusters(), conn=conn, disclosure_tier="shared_summary")
    assert _blackhole_policy_for_clusters(
        _clusters(), conn=conn, disclosure_tier="owner_raw"
    ) == _clusters()
    conn.close()


def test_no_black_holes_means_no_filtering(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "clean.db"))
    apply_all_migrations(conn)
    assert _blackhole_policy_for_clusters(
        _clusters(), conn=conn, disclosure_tier="shared_summary"
    ) == _clusters()
    conn.close()


# ------------------------------------------------------ the service reads


def _guard(conn, grantee: bool) -> BlackholeGuard:
    return BlackholeGuard(
        conn, caller_class=CallerClass.GRANTEE if grantee else CallerClass.OWNER_UI
    )


def test_the_service_requires_a_guard(corpus):
    """A forgotten call site must raise, not leak — the reads.py discipline."""
    from topos.features.signal.service import get_signal_service

    service = get_signal_service(conn=corpus.conn)
    with pytest.raises(TypeError):
        service.list_topic_clusters(limit=10)          # type: ignore[call-arg]
    with pytest.raises(TypeError):
        service.list_topic_cluster_members("tc-bh")    # type: ignore[call-arg]


def test_a_withheld_cluster_answers_exactly_like_a_missing_one(corpus, monkeypatch):
    """404 either way: confirming it exists but is hidden would leak (D5)."""
    from topos.features.signal import service as service_mod

    monkeypatch.setattr(service_mod, "get_db_connection", lambda: corpus.conn, raising=False)
    monkeypatch.setattr(
        "topos.core.state.get_db_connection", lambda: corpus.conn, raising=False
    )
    svc = service_mod.get_signal_service(conn=corpus.conn)
    with pytest.raises(LookupError):
        svc.list_topic_cluster_members("tc-bh", guard=_guard(corpus.conn, grantee=True))
    with pytest.raises(LookupError):
        svc.list_topic_cluster_members("tc-nonexistent", guard=_guard(corpus.conn, grantee=True))


def test_the_owner_still_gets_the_protected_cluster(corpus, monkeypatch):
    from topos.features.signal import service as service_mod

    monkeypatch.setattr(
        "topos.core.state.get_db_connection", lambda: corpus.conn, raising=False
    )
    svc = service_mod.get_signal_service(conn=corpus.conn)
    out = svc.list_topic_cluster_members("tc-bh", guard=_guard(corpus.conn, grantee=False))
    assert out["cluster_id"] == "tc-bh"


def test_total_counts_what_is_returned_not_what_exists(corpus, monkeypatch):
    """A total that disagrees with the rows confirms something was withheld."""
    from topos.features.signal import service as service_mod

    monkeypatch.setattr(
        "topos.core.state.get_db_connection", lambda: corpus.conn, raising=False
    )
    svc = service_mod.get_signal_service(conn=corpus.conn)
    out = svc.list_topic_clusters(guard=_guard(corpus.conn, grantee=True), limit=50)
    assert out["total"] == len(out["items"])
    labels = " ".join(str(i.get("label") or "") for i in out["items"])
    assert BH_CANONICAL not in labels


# ------------------------------- site 4: the fact reads that carry the label

def test_the_fact_readers_require_a_guard(corpus):
    """`top_topics` facts carry the cluster label as `tag`, and ordinary facts
    carry entity names in object_value — neither had a guard."""
    from topos.features.facts.reads import list_facts, list_stat_insights

    with pytest.raises(TypeError):
        list_facts(corpus.conn)                    # type: ignore[call-arg]
    with pytest.raises(TypeError):
        list_stat_insights(corpus.conn)            # type: ignore[call-arg]


def test_grantee_loses_facts_naming_a_protected_entity(corpus):
    from topos.features.facts.reads import list_facts

    out = list_facts(corpus.conn, guard=_guard(corpus.conn, grantee=True), limit=500)
    blob = str(out["items"])
    assert BH_CANONICAL not in blob
    assert corpus.tokens["fact_object_value"] not in blob


def test_owner_still_sees_them(corpus):
    from topos.features.facts.reads import list_facts

    owner = list_facts(corpus.conn, guard=_guard(corpus.conn, grantee=False), limit=500)
    grantee = list_facts(corpus.conn, guard=_guard(corpus.conn, grantee=True), limit=500)
    assert owner["total"] > grantee["total"], "the filter must actually remove something"


def test_total_and_rows_agree_after_filtering(corpus):
    """A total computed before the filter shifts when an entity is protected,
    which confirms it exists — the count side channel D5 removes."""
    from topos.features.facts.reads import list_facts

    out = list_facts(corpus.conn, guard=_guard(corpus.conn, grantee=True), limit=500)
    assert out["total"] == len(out["items"])
    assert sum(out["predicate_counts"].values()) <= out["total"] + len(out["items"])


def test_stat_insights_are_filtered_by_group_key_and_text(corpus):
    from topos.features.facts.reads import list_stat_insights

    out = list_stat_insights(corpus.conn, guard=_guard(corpus.conn, grantee=True))
    blob = str(out["items"])
    assert BH_CANONICAL not in blob
    assert corpus.tokens["stat_group_key"] not in blob
    assert out["total"] == len(out["items"])


def test_internal_scan_keys_never_reach_a_caller(corpus):
    """The filter lifts a payload blob onto each row; it must not be served."""
    from topos.features.facts.reads import list_facts

    out = list_facts(corpus.conn, guard=_guard(corpus.conn, grantee=False), limit=500)
    for item in out["items"]:
        assert "_scan_blob" not in item
