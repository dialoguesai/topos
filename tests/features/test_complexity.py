"""Tests for complexity analytics module (v2)."""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timedelta, timezone

import pytest

from topos.features.complexity.canonical import build_canonical_index
from topos.features.complexity.engine import (
    compute_complexity_snapshot,
    get_complexity_summary,
    get_influence_threads,
    get_shift_timeline,
)
from topos.features.complexity.entropy import (
    effective_count,
    entropy_to_focus_score,
    jaccard_distance,
    js_divergence_bits,
    normalized_entropy,
    shannon_entropy_bits,
)
from topos.features.complexity.focus import compute_focus_readings
from topos.features.complexity.projection import (
    AnalyticalProjection,
    ProjectionConfig,
    damped_source_sum,
    intent_weight,
)
from topos.features.complexity.store import ensure_complexity_tables, upsert_snapshot
from topos.features.complexity.topic_merge import build_supertopics


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _f32(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _conn() -> sqlite3.Connection:
    # check_same_thread=False mirrors the hub connection (handlers run the
    # engine in a worker thread via asyncio.to_thread)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE entities (
            entity_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            is_self INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT,
            mention_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE entity_edges (
            edge_id TEXT PRIMARY KEY,
            src_entity_id TEXT NOT NULL,
            dst_entity_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            last_event_at TEXT,
            valid_from TEXT,
            valid_to TEXT,
            created_at TEXT
        );
        CREATE TABLE entity_mentions (
            mention_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            source_id TEXT,
            canonical_table TEXT,
            surface_text TEXT,
            confidence REAL,
            event_at TEXT
        );
        CREATE TABLE topic_clusters (
            cluster_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            dimension TEXT NOT NULL DEFAULT 'memory',
            member_count INTEGER NOT NULL DEFAULT 0,
            label_terms_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE topic_cluster_members (
            member_id TEXT PRIMARY KEY,
            cluster_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            text_preview TEXT
        );
        CREATE TABLE timeline (
            event_at TEXT NOT NULL,
            record_id TEXT NOT NULL,
            source_id TEXT,
            canonical_table TEXT,
            cluster_id TEXT,
            entity_ids_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (event_at, record_id)
        );
        CREATE TABLE signal_embeddings (
            embedding_id TEXT PRIMARY KEY,
            record_id TEXT,
            vector_blob BLOB,
            vector_format TEXT NOT NULL DEFAULT 'f32',
            chunk_index INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE conversation_messages (
            message_id TEXT PRIMARY KEY,
            is_from_self INTEGER DEFAULT 0
        );
        """
    )
    return conn


def _seed_live_shaped(conn: sqlite3.Connection) -> None:
    """Fixture mirroring the live failure modes: duplicate entities across
    types, near-duplicate clusters across dimensions, empty
    timeline.entity_ids_json, passive-vs-active source mix, self entity,
    epoch-garbage timestamps."""
    now = _now()
    week = lambda i: _iso(now - timedelta(days=7 * i, hours=3))  # noqa: E731

    conn.executemany(
        """
        INSERT INTO entities
        (entity_id, entity_type, canonical_name, normalized_name, aliases_json,
         is_self, first_seen, last_seen, mention_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("self1", "person", "Owner", "owner", "[]", 1, week(10), week(0), 50),
            ("p1", "person", "Alice", "alice", "[]", 0, week(10), week(0), 12),
            ("p2", "person", "Bob", "bob", "[]", 0, week(9), week(1), 6),
            # duplicate person (same name, extracted twice)
            ("p3", "person", "Carol Reyes", "carol reyes", "[]", 0, week(8), week(1), 4),
            ("p4", "person", "Carol Reyes", "carol reyes", "[]", 0, week(6), week(0), 2),
            # same concept split across types (live: "topos" x4)
            ("c1", "org", "Aurora", "aurora", "[]", 0, week(10), week(0), 10),
            ("c2", "product", "Aurora", "aurora", "[]", 0, week(7), week(0), 5),
            ("c3", "project", "Aurora", "aurora", "[]", 0, week(9), week(1), 7),
            ("g1", "goal", "Ship Aurora v2", "ship aurora v2", "[]", 0, week(5), week(0), 0),
            ("conv1", "conversation", "Chat with Bob", "chat with bob", "[]", 0, week(4), week(2), 0),
            ("pl1", "place", "Austin", "austin", "[]", 0, week(9), week(3), 3),
        ],
    )
    conn.executemany(
        """
        INSERT INTO entity_edges
        (edge_id, src_entity_id, dst_entity_id, edge_type, weight, evidence_count,
         last_event_at, valid_from, valid_to, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("e1", "p1", "c1", "worked_on", 5.0, 8, week(1), week(9), None, week(9)),
            ("e2", "p1", "p2", "communicates_with", 3.0, 6, week(0), week(8), None, week(8)),
            ("e3", "p2", "c2", "mentions", 1.0, 2, week(2), week(6), None, week(6)),
            ("e4", "p3", "c3", "discusses", 2.0, 3, week(3), week(6), None, week(6)),
            ("e5", "p4", "pl1", "located_at", 2.0, 1, week(4), week(5), None, week(5)),
            ("e6", "self1", "g1", "pursues", 2.0, 1, week(1), week(5), None, week(5)),
            # duplicate-artifact edge: canonicalizes to a self-loop, must drop
            ("e7", "c1", "c2", "relates_to", 1.0, 1, week(2), week(7), None, week(7)),
        ],
    )

    records: list[tuple[str, str, str, str]] = []
    mentions: list[tuple[str, str, str, str, str, float, str]] = []
    members: list[tuple[str, str, str, str]] = []
    embeddings: list[tuple[str, str, bytes]] = []

    def add_record(
        record_id: str,
        source_id: str,
        table: str,
        when: str,
        cluster_ids: list[str],
        entity_ids: list[str],
        vector: list[float],
    ) -> None:
        records.append((when, record_id, source_id, table))
        for cluster_id in cluster_ids:
            members.append((f"m_{cluster_id}_{record_id}", cluster_id, record_id, source_id))
        for entity_id in entity_ids:
            mentions.append(
                (
                    f"men_{entity_id}_{record_id}",
                    entity_id,
                    record_id,
                    source_id,
                    table,
                    0.9,
                    when,
                )
            )
        embeddings.append((f"emb_{record_id}", record_id, _f32(vector)))

    # Two genuinely distinct themes, each duplicated across two "dimensions"
    # (ca/cb ~ "Aurora work", cc/cd ~ "Health"), plus one unrelated cluster.
    v_aurora = [1.0, 0.0, 0.1, 0.0]
    v_health = [0.0, 1.0, 0.0, 0.1]
    v_misc = [0.5, 0.5, 0.7, 0.2]
    for i in range(8):
        add_record(
            f"r_aurora_{i}",
            "github_activity" if i % 2 == 0 else "grow_journal",
            "activity_events" if i % 2 == 0 else "journal_entries",
            week(i),
            ["ca", "cb"],
            ["p1", "c1"] if i % 2 == 0 else ["p1", "c3"],
            [v + 0.01 * i for v in v_aurora],
        )
    for i in range(6):
        add_record(
            f"r_health_{i}",
            "imessage",
            "conversation_messages",
            week(i),
            ["cc", "cd"],
            ["p2"] if i % 2 == 0 else ["p2", "p3"],
            [v + 0.01 * i for v in v_health],
        )
    for i in range(3):
        add_record(
            f"r_misc_{i}",
            "browser_visits",
            "activity_events",
            week(i),
            ["ce"],
            ["p4"] if i == 0 else [],
            [v + 0.02 * i for v in v_misc],
        )

    conn.executemany(
        "INSERT INTO timeline (event_at, record_id, source_id, canonical_table) VALUES (?, ?, ?, ?)",
        records,
    )
    # epoch garbage must be ignored
    conn.execute(
        "INSERT INTO timeline (event_at, record_id, source_id, canonical_table) VALUES (?, ?, ?, ?)",
        ("1970-01-01T00:00:01+00:00", "r_epoch", "imessage", "conversation_messages"),
    )
    conn.executemany(
        """
        INSERT INTO entity_mentions
        (mention_id, entity_id, record_id, source_id, canonical_table, confidence, event_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        mentions,
    )
    conn.executemany(
        "INSERT INTO topic_cluster_members (member_id, cluster_id, record_id, source_id) VALUES (?, ?, ?, ?)",
        members,
    )
    conn.executemany(
        "INSERT INTO signal_embeddings (embedding_id, record_id, vector_blob) VALUES (?, ?, ?)",
        embeddings,
    )
    conn.executemany(
        "INSERT INTO topic_clusters (cluster_id, label, dimension, member_count, label_terms_json) VALUES (?, ?, ?, ?, ?)",
        [
            ("ca", "Aurora Project", "memory", 8, '["aurora","build","ship"]'),
            ("cb", "Aurora Project", "relationships", 8, '["aurora","build","team"]'),
            ("cc", "Health", "wellbeing", 6, '["sleep","exercise"]'),
            ("cd", "Wellness Routines", "memory", 6, '["sleep","exercise","routine"]'),
            ("ce", "Misc Browsing", "interests", 3, '["news","random"]'),
        ],
    )
    conn.executemany(
        "INSERT INTO conversation_messages (message_id, is_from_self) VALUES (?, ?)",
        [(f"r_health_{i}", i % 2) for i in range(6)],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# helpers / entropy
# ---------------------------------------------------------------------------


def test_entropy_helpers():
    assert shannon_entropy_bits([1.0, 1.0]) == pytest.approx(1.0, abs=0.01)
    assert entropy_to_focus_score(0.0) == pytest.approx(100.0, abs=0.01)
    assert jaccard_distance(["a", "b"], ["b", "c"]) == pytest.approx(2 / 3, abs=0.01)
    p = {"a": 0.5, "b": 0.5}
    q = {"a": 0.9, "b": 0.1}
    assert js_divergence_bits(p, q) > 0


def test_normalized_entropy_no_saturation():
    # v1 failure mode: any distribution over many items clipped to focus 0.
    uniform_many = [1.0] * 200
    assert normalized_entropy(uniform_many) == pytest.approx(1.0, abs=0.001)
    skewed_many = [100.0] + [0.5] * 199
    assert normalized_entropy(skewed_many) < 0.7
    assert normalized_entropy([5.0]) == 0.0
    assert effective_count([1.0, 1.0, 1.0, 1.0]) == pytest.approx(4.0, abs=0.01)


def test_damped_source_sum_diminishing_returns():
    # 40 browser hits from one source must not beat 3 independent sources.
    one_source = damped_source_sum([("browser", 0.2)] * 40)
    independent = damped_source_sum([("journal", 1.0), ("github", 0.9), ("calendar", 1.0)])
    assert independent > one_source


def test_intent_weights_ordering():
    assert intent_weight("grow_journal") > intent_weight("imessage") > intent_weight("browser_visits")
    assert intent_weight(None, None, is_from_self=True) > intent_weight(None, None, is_from_self=False)


# ---------------------------------------------------------------------------
# canonicalization
# ---------------------------------------------------------------------------


def test_canonical_topic_does_not_bridge_incompatible_types():
    """Loop-5 review: a same-name topic entity must not transitively merge a
    person with an org/place (live examples: Lindy, nyc)."""
    conn = _conn()
    conn.executemany(
        """
        INSERT INTO entities
        (entity_id, entity_type, canonical_name, normalized_name, mention_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("lp", "person", "Lindy", "lindy", 5),
            ("lo", "org", "Lindy", "lindy", 21),
            ("lt", "topic", "Lindy", "lindy", 2),
        ],
    )
    conn.commit()
    index = build_canonical_index(conn)
    # person and org stay separate
    assert index.resolve("lp") != index.resolve("lo")
    # topic attaches to exactly one concrete entity (person has priority)
    assert index.resolve("lt") == index.resolve("lp")


def test_baseline_gates_on_current_window_usability():
    from topos.features.complexity.focus import _baseline_summary

    prior = [
        {"score": 50.0 + i, "evidence_total": 20.0, "usable": True} for i in range(6)
    ]
    quiet_current = {"score": 100.0, "evidence_total": 0.5, "usable": False}
    result = _baseline_summary(prior + [quiet_current])
    assert result["status"] == "insufficient_evidence"
    assert "percentile" not in result


def test_self_override_scoped_to_generic_conversation_sources():
    # voxterm transcripts are self-authored speech (0.8) and must not be
    # demoted by the sent/received refinement
    assert intent_weight("voxterm_transcripts", "conversation_messages", is_from_self=True) == 0.8
    assert intent_weight("voxterm_transcripts", "conversation_messages", is_from_self=False) == 0.8
    assert intent_weight("imessage", "conversation_messages", is_from_self=True) == 0.7
    assert intent_weight("imessage", "conversation_messages", is_from_self=False) == 0.4


def test_volatility_empty_week_transition_scores_max_shift():
    from topos.features.complexity.volatility import _distribution_shift

    assert _distribution_shift(None, {"a": 1.0}) == 0.0  # first week: no prior
    assert _distribution_shift({}, {"a": 1.0}) == 1.0  # activity restarts
    assert _distribution_shift({"a": 1.0}, {}) == 1.0  # activity stops
    assert _distribution_shift({}, {}) == 0.0


def test_windowed_projection_excludes_historical_edges():
    conn = _conn()
    _seed_live_shaped(conn)
    canonical = build_canonical_index(conn)
    supertopics = build_supertopics(conn)
    windowed = AnalyticalProjection(
        conn, ProjectionConfig(window_days=7), canonical=canonical, supertopics=supertopics
    )
    lifetime = AnalyticalProjection(
        conn, ProjectionConfig(window_days=None), canonical=canonical, supertopics=supertopics
    )
    assert len(windowed.edges()) < len(lifetime.edges())
    week_ago = _now() - timedelta(days=7)
    for edge in windowed.edges():
        assert edge.event_at is None or edge.event_at >= week_ago


def test_canonical_merges_duplicates():
    conn = _conn()
    _seed_live_shaped(conn)
    index = build_canonical_index(conn)
    # same-name persons merge
    assert index.resolve("p3") == index.resolve("p4")
    # same-name concept types merge (org/product/project)
    assert index.resolve("c1") == index.resolve("c2") == index.resolve("c3")
    canonical = index.get(index.resolve("c1"))
    assert canonical.canonical_type == "org"
    assert len(canonical.members) == 3
    # goals and conversations never name-merge and are attention-excluded
    goal = index.get(index.resolve("g1"))
    assert goal.attention_eligible is False
    self_entity = index.get(index.resolve("self1"))
    assert self_entity.is_self and not self_entity.attention_eligible


# ---------------------------------------------------------------------------
# topic merging
# ---------------------------------------------------------------------------


def test_supertopics_label_merge_without_record_overlap():
    """Live loop-2 finding: per-dimension clusterings share NO records, so
    same-label clusters must merge on label + weak semantic corroboration."""
    conn = _conn()
    now = _now()
    week = lambda i: _iso(now - timedelta(days=7 * i))  # noqa: E731
    conn.executemany(
        "INSERT INTO topic_clusters (cluster_id, label, dimension, member_count, label_terms_json) VALUES (?, ?, ?, ?, ?)",
        [
            ("x1", "Personal Projects", "memory", 3, '["code"]'),
            ("x2", "Personal Projects", "relationships", 3, '["team"]'),
            ("x3", "Cooking", "wellbeing", 3, '["food"]'),
        ],
    )
    rows = []
    embeddings = []
    members = []
    similar = [1.0, 0.2, 0.0]
    different = [0.0, 0.1, 1.0]
    for i in range(3):
        for cluster_id, vec in (("x1", similar), ("x2", similar), ("x3", different)):
            record_id = f"rec_{cluster_id}_{i}"
            rows.append((week(i), record_id, "imessage", "conversation_messages"))
            members.append((f"m_{record_id}", cluster_id, record_id, "imessage"))
            embeddings.append((f"e_{record_id}", record_id, _f32([v + 0.02 * i for v in vec])))
    conn.executemany(
        "INSERT INTO timeline (event_at, record_id, source_id, canonical_table) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO topic_cluster_members (member_id, cluster_id, record_id, source_id) VALUES (?, ?, ?, ?)",
        members,
    )
    conn.executemany(
        "INSERT INTO signal_embeddings (embedding_id, record_id, vector_blob) VALUES (?, ?, ?)",
        embeddings,
    )
    conn.commit()
    index = build_supertopics(conn)
    # zero record overlap but same label + high centroid cosine -> merged
    assert index.resolve("x1") == index.resolve("x2")
    assert index.resolve("x1") != index.resolve("x3")


def test_entity_lens_active_share_floor():
    """Junk one-mention entities must not count as active in a lens."""
    conn = _conn()
    _seed_live_shaped(conn)
    # add 80 junk orgs with a single low-weight browser mention each, so each
    # holds well under 1% of the lens's evidence
    now = _now()
    when = _iso(now - timedelta(days=2))
    for i in range(80):
        conn.execute(
            "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, mention_count) VALUES (?, 'org', ?, ?, 1)",
            (f"junk{i}", f"JunkCorp {i}", f"junkcorp {i}"),
        )
        conn.execute(
            "INSERT INTO timeline (event_at, record_id, source_id, canonical_table) VALUES (?, ?, 'browser_visits', 'activity_events')",
            (when, f"r_junk_{i}"),
        )
        conn.execute(
            """
            INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table, confidence, event_at)
            VALUES (?, ?, ?, 'browser_visits', 'activity_events', 0.6, ?)
            """,
            (f"men_junk_{i}", f"junk{i}", f"r_junk_{i}", when),
        )
    # one strong org so the lens has a dominant member
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, mention_count) VALUES ('bigorg', 'org', 'BigOrg', 'bigorg', 20)"
    )
    for i in range(10):
        when_i = _iso(now - timedelta(days=1 + i))
        conn.execute(
            """
            INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table, confidence, event_at)
            VALUES (?, 'bigorg', ?, 'grow_journal', 'journal_entries', 0.95, ?)
            """,
            (f"men_big_{i}", f"r_big_{i}", when_i),
        )
    conn.commit()
    canonical = build_canonical_index(conn)
    supertopics = build_supertopics(conn)
    readings = compute_focus_readings(conn, canonical, supertopics)
    lenses = readings["current_focus"]["entity_lenses"]
    # the 80 junk orgs each hold < 1% of lens evidence and are filtered
    assert lenses["organizations"]["active_count"] < 20


def test_supertopics_merge_cross_dimension_duplicates():
    conn = _conn()
    _seed_live_shaped(conn)
    index = build_supertopics(conn)
    assert index.raw_cluster_count == 5
    # ca/cb and cc/cd collapse, ce stays
    assert index.resolve("ca") == index.resolve("cb")
    assert index.resolve("cc") == index.resolve("cd")
    assert index.resolve("ca") != index.resolve("cc")
    assert len(index.supertopics) == 3
    merged = index.supertopics[index.resolve("ca")]
    # records deduped across the two dimensions
    assert merged.member_count == 8


# ---------------------------------------------------------------------------
# projection
# ---------------------------------------------------------------------------


def test_projection_excludes_epoch_and_canonicalizes():
    conn = _conn()
    _seed_live_shaped(conn)
    canonical = build_canonical_index(conn)
    supertopics = build_supertopics(conn)
    projection = AnalyticalProjection(
        conn, ProjectionConfig(window_days=90), canonical=canonical, supertopics=supertopics
    )
    records = projection.records()
    assert "r_epoch" not in records
    # canonical-self-loop edge (c1->c2) dropped
    for edge in projection.edges():
        assert edge.src != edge.dst
    evidence = projection.entity_evidence()
    aurora = canonical.resolve("c1")
    assert aurora in evidence
    # authored github/journal evidence for Aurora outweighs browser misc
    misc = canonical.resolve("p4")
    assert evidence[aurora].weight > evidence.get(misc).weight if misc in evidence else True


# ---------------------------------------------------------------------------
# focus readings
# ---------------------------------------------------------------------------


def test_focus_readings_not_saturated_and_structured():
    conn = _conn()
    _seed_live_shaped(conn)
    canonical = build_canonical_index(conn)
    supertopics = build_supertopics(conn)
    readings = compute_focus_readings(conn, canonical, supertopics)
    focus = readings["current_focus"]
    assert 0.0 < focus["score"] < 100.0
    assert focus["effective_topics"] > 0
    assert "30d" in focus["windows"] and "365d" in focus["windows"]
    lenses = focus["entity_lenses"]
    assert "people" in lenses
    breadth = readings["information_breadth"]
    assert breadth["sub_metrics"]["effective_topics_now"] > 0
    pipeline = readings["pipeline_confidence"]
    assert 0.0 <= pipeline["score"] <= 100.0
    assert isinstance(pipeline["flags"], list)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_store_roundtrip():
    conn = _conn()
    ensure_complexity_tables(conn)
    upsert_snapshot(
        conn,
        snapshot_id="test_snapshot",
        week_start="2026-03-03",
        metric_set="full",
        metrics={"focus_index": {"score": 42}},
    )
    row = conn.execute(
        "SELECT metrics_json FROM complexity_snapshots WHERE week_start = ?",
        ("2026-03-03",),
    ).fetchone()
    assert row is not None
    payload = json.loads(row["metrics_json"])
    assert payload["focus_index"]["score"] == 42


# ---------------------------------------------------------------------------
# engine integration
# ---------------------------------------------------------------------------


def test_compute_complexity_snapshot_full():
    conn = _conn()
    _seed_live_shaped(conn)
    snapshot = compute_complexity_snapshot(conn)
    # v1-compatible keys
    for key in ("topic_quality", "focus_index", "structure_clarity", "shift_detector", "influence_threads"):
        assert key in snapshot
    assert 0.0 < snapshot["focus_index"]["score"] < 100.0
    # v2 readings
    readings = snapshot["readings"]
    for key in ("current_focus", "structural_clarity", "information_breadth", "pipeline_confidence"):
        assert key in readings
    assert snapshot["canonicalization"]["merge_rate"] > 0
    assert snapshot["topic_quality"]["supertopics"]["merge_rate"] > 0


def test_get_complexity_summary_recomputes_v1_cache():
    conn = _conn()
    _seed_live_shaped(conn)
    ensure_complexity_tables(conn)
    upsert_snapshot(
        conn,
        snapshot_id="summary_latest",
        metric_set="summary",
        metrics={"focus_index": {"score": 0.0}},  # stale v1 snapshot
    )
    summary = get_complexity_summary(conn)
    assert int(summary.get("engine_version") or 0) >= 2


def test_get_shift_timeline():
    conn = _conn()
    _seed_live_shaped(conn)
    result = get_shift_timeline(conn)
    assert isinstance(result, dict)
    timeline = result.get("timeline") or []
    assert timeline, "shift timeline should not be empty on live-shaped data"
    assert any(row["active_topics"] > 0 for row in timeline)


def test_get_influence_threads_live_shaped():
    conn = _conn()
    _seed_live_shaped(conn)
    result = get_influence_threads(conn, recompute=True)
    threads = result["threads"]
    assert threads, "influence threads must not be empty on live-shaped data"
    for thread in threads:
        assert thread["epistemic_status"] in {
            "direct_evidence",
            "strongly_supported",
            "structural_association",
            "speculative_similarity",
        }
        assert len(thread["path"]) >= 2
        assert thread["strength"] > 0
    # the self entity must not appear in threads
    labels = {t["source_label"] for t in threads} | {t["target_label"] for t in threads}
    assert "Owner" not in labels


def test_influence_target_mode():
    conn = _conn()
    _seed_live_shaped(conn)
    result = get_influence_threads(conn, target="Aurora")
    assert result.get("target") == "Aurora"
    threads = result["threads"]
    assert threads
    for thread in threads:
        assert thread["target_label"] == "Aurora"


# ---------------------------------------------------------------------------
# graceful degradation on minimal/empty schemas
# ---------------------------------------------------------------------------


def test_empty_database_degrades_gracefully():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    snapshot = compute_complexity_snapshot(conn)
    assert snapshot["focus_index"]["score"] == 0.0
    assert snapshot["influence_threads"] == []
