"""Source-blind evidence shaping (TOPOS_EVIDENCE_SHAPING, default off).

Measured on the owner's snapshot (2026-10-21, 112 questions, 548 turns): the vector
lane held the answering record in its top five for 143 of the 164 known-item turns
that lost it, and vector items filled 0.2% of the fused top 20. Stat, fact,
derived-object and entity items filled most of the rest. Separately, one AI coding
session holds ~30–85 index rows, so one long session can stand for many hits.

Each rule here is source-blind: it reads relevance, text, conversation identity and
time, never which connector a row came from. Relevance is per question, so no rule
sets a per-source quota.

1. Conversation grouping: one conversation (or a short session) is one witness.
2. Near-duplicate removal: the same words twice are one hit.
3. Relevance floor: a hit far below the best one does not pad the top k.
4. Bounded derived share: derived items cannot take more than a set share of any
   prefix of the fused list while source records remain to fill it.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import pytest

from topos.features.signal import vector_settings
from topos.query import evidence_shaping as es
from topos.query.retrieval import _rrf_fuse_summary_lists


def _hit(
    record_id: str,
    *,
    text: Optional[str] = None,
    similarity: Optional[float] = 0.6,
    source_id: str = "src_a",
    event_at: Optional[str] = None,
) -> Dict[str, Any]:
    body = text if text is not None else f"distinct passage about subject {record_id} number {record_id}"
    return {
        "record_id": record_id,
        "text_preview": body[:200],
        "search_text": body,
        "similarity": similarity,
        "source_id": source_id,
        "event_at": event_at,
        "record_type": "ai_chat_message",
    }


def _ids(hits: List[Dict[str, Any]]) -> List[str]:
    return [str(h["record_id"]) for h in hits]


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


class TestSwitch:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("TOPOS_EVIDENCE_SHAPING", raising=False)
        assert vector_settings.evidence_shaping_enabled() is False

    def test_on_when_set(self, monkeypatch):
        monkeypatch.setenv("TOPOS_EVIDENCE_SHAPING", "on")
        assert vector_settings.evidence_shaping_enabled() is True

    def test_pool_is_wider_than_the_lane_and_clamped(self, monkeypatch):
        monkeypatch.delenv("TOPOS_EVIDENCE_POOL", raising=False)
        assert vector_settings.evidence_pool_limit() == 60
        monkeypatch.setenv("TOPOS_EVIDENCE_POOL", "5000")
        assert vector_settings.evidence_pool_limit() == 100
        monkeypatch.setenv("TOPOS_EVIDENCE_POOL", "3")
        assert vector_settings.evidence_pool_limit() == 20

    def test_shape_vector_hits_is_identity_when_off(self, monkeypatch):
        monkeypatch.delenv("TOPOS_EVIDENCE_SHAPING", raising=False)
        hits = [_hit(f"r{i}") for i in range(30)]
        assert es.shape_vector_hits(hits, conn=None, limit=20) is hits


# ---------------------------------------------------------------------------
# Rule 1 — conversation grouping
# ---------------------------------------------------------------------------


class TestConversationGrouping:
    def test_one_long_session_is_one_witness(self):
        hits = [_hit(f"a{i}") for i in range(10)] + [_hit("b0"), _hit("c0")]
        keys = ["conv:a"] * 10 + ["conv:b", "conv:c"]
        out = es.group_by_conversation(hits, keys, limit=3, passages=1, bonus=0.05)
        assert _ids(out) == ["a0", "b0", "c0"]

    def test_heads_of_every_group_come_before_second_passages(self):
        hits = [_hit("a0"), _hit("a1"), _hit("b0"), _hit("b1"), _hit("c0")]
        keys = ["conv:a", "conv:a", "conv:b", "conv:b", "conv:c"]
        out = es.group_by_conversation(hits, keys, limit=10, passages=2, bonus=0.0)
        assert _ids(out) == ["a0", "b0", "c0", "a1", "b1"]

    def test_passages_cap_per_group(self):
        hits = [_hit(f"a{i}") for i in range(5)]
        out = es.group_by_conversation(hits, ["conv:a"] * 5, limit=10, passages=2, bonus=0.0)
        assert _ids(out) == ["a0", "a1"]

    def test_supporting_hits_lift_a_close_group(self):
        # b's best hit is one place behind a's, but b has three distinct supporters.
        hits = [_hit("a0"), _hit("b0"), _hit("b1"), _hit("b2"), _hit("b3")]
        keys = ["conv:a", "conv:b", "conv:b", "conv:b", "conv:b"]
        out = es.group_by_conversation(hits, keys, limit=2, passages=1, bonus=0.05)
        assert _ids(out) == ["b0", "a0"]

    def test_the_bonus_is_small(self):
        # Support never lifts a group whose best hit is far behind: rank 0 vs rank 8.
        hits = [_hit("a0")] + [_hit(f"x{i}") for i in range(7)] + [_hit(f"b{i}") for i in range(4)]
        keys = ["conv:a"] + [f"conv:x{i}" for i in range(7)] + ["conv:b"] * 4
        out = es.group_by_conversation(hits, keys, limit=1, passages=1, bonus=0.05)
        assert _ids(out) == ["a0"]

    def test_support_counts_at_most_three(self):
        # Ten supporters lift no more than three: from rank 4 the group passes rank 3
        # (1/14 * 1.15 > 1/13) but not rank 2. Uncapped (1/14 * 1.5) it would lead.
        hits = [_hit("x0"), _hit("x1"), _hit("y0"), _hit("x3")] + [_hit(f"b{i}") for i in range(11)]
        keys = ["conv:x0", "conv:x1", "conv:y0", "conv:x3"] + ["conv:b"] * 11
        out = es.group_by_conversation(hits, keys, limit=5, passages=1, bonus=0.05)
        assert _ids(out) == ["x0", "x1", "y0", "b0", "x3"]

    def test_limit_is_respected(self):
        hits = [_hit(f"r{i}") for i in range(40)]
        out = es.group_by_conversation(hits, [f"k{i}" for i in range(40)], limit=20, passages=2, bonus=0.05)
        assert len(out) == 20
        assert _ids(out) == [f"r{i}" for i in range(20)]

    def test_grouping_adds_no_keys_to_hits(self):
        # Conversation ids can embed phone numbers; they must never ride out on a hit.
        hits = [_hit("a0"), _hit("a1")]
        before = [set(h) for h in hits]
        out = es.group_by_conversation(hits, ["conv:a", "conv:a"], limit=5, passages=2, bonus=0.05)
        assert [set(h) for h in out] == before


class TestGroupKeys:
    def test_conversation_id_wins(self):
        hits = [_hit("r1"), _hit("r2"), _hit("r3")]
        keys = es.group_keys(hits, {"r1": "c1", "r2": "c1", "r3": "c2"}, gap_minutes=30)
        assert keys[0] == keys[1] != keys[2]

    def test_same_conversation_id_in_two_sources_is_two_groups(self):
        hits = [_hit("r1", source_id="s1"), _hit("r2", source_id="s2")]
        keys = es.group_keys(hits, {"r1": "c1", "r2": "c1"}, gap_minutes=30)
        assert keys[0] != keys[1]

    def test_hits_without_conversation_group_by_session_gap(self):
        hits = [
            _hit("v1", event_at="2026-10-01T10:00:00+00:00"),
            _hit("v2", event_at="2026-10-01T10:20:00+00:00"),
            _hit("v3", event_at="2026-10-01T10:45:00+00:00"),  # chained: 25 min after v2
            _hit("v4", event_at="2026-10-01T12:00:00+00:00"),  # 75 min gap: new session
        ]
        keys = es.group_keys(hits, {}, gap_minutes=30)
        assert keys[0] == keys[1] == keys[2] != keys[3]

    def test_session_gap_zero_keeps_every_hit_apart(self):
        hits = [
            _hit("v1", event_at="2026-10-01T10:00:00+00:00"),
            _hit("v2", event_at="2026-10-01T10:00:00+00:00"),  # same instant
            _hit("v3", event_at="2026-10-01T10:01:00+00:00"),
        ]
        keys = es.group_keys(hits, {}, gap_minutes=0)
        assert len(set(keys)) == 3

    def test_sessions_never_span_sources(self):
        hits = [
            _hit("v1", source_id="s1", event_at="2026-10-01T10:00:00+00:00"),
            _hit("v2", source_id="s2", event_at="2026-10-01T10:01:00+00:00"),
        ]
        keys = es.group_keys(hits, {}, gap_minutes=30)
        assert keys[0] != keys[1]

    def test_undated_hit_without_conversation_stands_alone(self):
        hits = [_hit("v1"), _hit("v2")]
        keys = es.group_keys(hits, {}, gap_minutes=30)
        assert keys[0] != keys[1]

    def test_lookup_reads_the_index_and_never_the_hit(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE signal_embeddings (record_id TEXT, source_id TEXT, conversation_id TEXT, chunk_index INT)"
        )
        conn.executemany(
            "INSERT INTO signal_embeddings VALUES (?,?,?,?)",
            [("r1", "s", "c1", 0), ("r1", "s", "c1", 1), ("r2", "s", "c1", 0), ("r3", "s", None, 0)],
        )
        found = es.lookup_conversation_ids(conn, ["r1", "r2", "r3", "missing"])
        assert found == {"r1": "c1", "r2": "c1"}

    def test_lookup_without_a_connection_is_empty(self):
        assert es.lookup_conversation_ids(None, ["r1"]) == {}


# ---------------------------------------------------------------------------
# Rule 2 — near-duplicate removal
# ---------------------------------------------------------------------------


class TestNearDuplicates:
    TEXT = "we decided to move the release to thursday after the migration check passed on the copy"

    def test_near_identical_text_keeps_the_better_ranked_copy(self):
        hits = [
            _hit("first", text=self.TEXT, source_id="s1"),
            _hit("other", text="a completely different passage about lunch plans downtown"),
            _hit("twin", text=self.TEXT + " ok", source_id="s2"),
        ]
        out = es.drop_near_duplicates(hits, threshold=0.8)
        assert _ids(out) == ["first", "other"]

    def test_distinct_text_is_kept(self):
        hits = [_hit(f"r{i}") for i in range(6)]
        assert _ids(es.drop_near_duplicates(hits, threshold=0.8)) == [f"r{i}" for i in range(6)]

    def test_short_text_compares_whole(self):
        hits = [_hit("a", text="GitHub"), _hit("b", text="github "), _hit("c", text="GitLab")]
        assert _ids(es.drop_near_duplicates(hits, threshold=0.8)) == ["a", "c"]

    def test_threshold_zero_disables(self):
        hits = [_hit("a", text=self.TEXT), _hit("b", text=self.TEXT)]
        assert _ids(es.drop_near_duplicates(hits, threshold=0.0)) == ["a", "b"]

    def test_jaccard_at_threshold_is_a_duplicate(self):
        a = set(es._shingles("one two three four five six"))
        b = set(es._shingles("one two three four five seven"))
        j = len(a & b) / len(a | b)
        hits = [_hit("a", text="one two three four five six"), _hit("b", text="one two three four five seven")]
        assert _ids(es.drop_near_duplicates(hits, threshold=j)) == ["a"]
        assert _ids(es.drop_near_duplicates(hits, threshold=j + 1e-9)) == ["a", "b"]


# ---------------------------------------------------------------------------
# Rule 3 — relevance floor
# ---------------------------------------------------------------------------


class TestRelevanceFloor:
    def test_far_neighbours_are_dropped(self):
        hits = [_hit("top", similarity=0.70), _hit("near", similarity=0.60), _hit("far", similarity=0.40)]
        assert _ids(es.apply_relevance_floor(hits, margin=0.15)) == ["top", "near"]

    def test_boundary_is_kept(self):
        hits = [_hit("top", similarity=0.70), _hit("edge", similarity=0.55)]
        assert _ids(es.apply_relevance_floor(hits, margin=0.15)) == ["top", "edge"]

    def test_keyword_only_hits_carry_no_cosine_and_stay(self):
        hits = [_hit("top", similarity=0.70), _hit("fts", similarity=None), _hit("far", similarity=0.30)]
        assert _ids(es.apply_relevance_floor(hits, margin=0.15)) == ["top", "fts"]

    def test_floor_is_relative_to_the_best_cosine_not_the_first_hit(self):
        # Hybrid / rerank order can put a lower cosine first.
        hits = [_hit("first", similarity=0.50), _hit("best", similarity=0.80)]
        assert _ids(es.apply_relevance_floor(hits, margin=0.15)) == ["best"]

    def test_margin_zero_disables(self):
        hits = [_hit("top", similarity=0.9), _hit("far", similarity=0.3)]
        assert _ids(es.apply_relevance_floor(hits, margin=0.0)) == ["top", "far"]


# ---------------------------------------------------------------------------
# Rules 1–3 together, through the settings
# ---------------------------------------------------------------------------


class TestShapeVectorHits:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("TOPOS_EVIDENCE_SHAPING", "on")
        for name in (
            "TOPOS_EVIDENCE_GROUP_PASSAGES", "TOPOS_EVIDENCE_GROUP_BONUS",
            "TOPOS_EVIDENCE_SESSION_GAP_MINUTES", "TOPOS_EVIDENCE_DUPLICATE_JACCARD",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("TOPOS_EVIDENCE_FLOOR_MARGIN", "0.2")

    def _conn(self, rows):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE signal_embeddings (record_id TEXT, source_id TEXT, conversation_id TEXT, chunk_index INT)"
        )
        conn.executemany("INSERT INTO signal_embeddings VALUES (?,?,?,0)", rows)
        return conn

    def test_a_crowded_conversation_yields_its_lane_slots(self):
        hits = [_hit(f"a{i}", similarity=0.70 - i * 0.001) for i in range(30)]
        hits += [_hit(f"o{i}", similarity=0.60 - i * 0.001) for i in range(10)]
        conn = self._conn([(f"a{i}", "src_a", "long") for i in range(30)]
                          + [(f"o{i}", "src_a", f"other{i}") for i in range(10)])
        out = es.shape_vector_hits(hits, conn=conn, limit=20)
        assert sum(1 for h in out if h["record_id"].startswith("a")) == 2
        assert {f"o{i}" for i in range(10)} <= set(_ids(out))

    def test_source_blind_relabelling_the_sources_changes_nothing(self):
        hits = [_hit(f"a{i}", similarity=0.7 - i * 0.01) for i in range(12)]
        rows = [(f"a{i}", "src_a", f"c{i % 4}") for i in range(12)]
        out_a = es.shape_vector_hits([dict(h) for h in hits], conn=self._conn(rows), limit=8)
        relabelled = [{**h, "source_id": "src_z"} for h in hits]
        rows_z = [(rid, "src_z", conv) for rid, _, conv in rows]
        out_z = es.shape_vector_hits(relabelled, conn=self._conn(rows_z), limit=8)
        assert _ids(out_a) == _ids(out_z)

    def test_passages_zero_turns_grouping_off(self, monkeypatch):
        monkeypatch.setenv("TOPOS_EVIDENCE_GROUP_PASSAGES", "0")
        hits = [_hit(f"a{i}", similarity=0.7 - i * 0.001) for i in range(30)]
        conn = self._conn([(f"a{i}", "src_a", "long") for i in range(30)])
        assert _ids(es.shape_vector_hits(hits, conn=conn, limit=20)) == [f"a{i}" for i in range(20)]

    def test_never_more_than_the_limit(self):
        hits = [_hit(f"r{i}") for i in range(60)]
        assert len(es.shape_vector_hits(hits, conn=None, limit=20)) == 20


# ---------------------------------------------------------------------------
# Rule 4 — bounded derived share in fusion
# ---------------------------------------------------------------------------


def _item(rid: str, source: str) -> Dict[str, Any]:
    return {"record_id": rid, "topic": rid, "summary_text": rid, "retrieval_source": source}


def _lists(n_derived: int = 30, n_vector: int = 6):
    stats = [_item(f"stat{i}", "stat_insight") for i in range(n_derived)]
    facts = [_item(f"fact{i}", "fact") for i in range(n_derived)]
    vector = [_item(f"vec{i}", "vector") for i in range(n_vector)]
    return [("stat_insights", 2.0, stats), ("facts_store", 1.5, facts), ("vector", 1.0, vector)]


def _kinds(fused: List[Dict[str, Any]]) -> List[str]:
    return ["R" if str(i["record_id"]).startswith("vec") else "D" for i in fused]


class TestBoundedDerivedShare:
    def test_without_the_bound_derived_lanes_fill_the_list(self):
        fused = _rrf_fuse_summary_lists(_lists(), cap=20)
        assert _kinds(fused).count("R") == 0

    def test_every_prefix_respects_the_share_while_records_remain(self):
        fused = _rrf_fuse_summary_lists(_lists(), cap=20, derived_share=0.5)
        kinds = _kinds(fused)
        assert kinds.count("R") == 6
        for n in range(1, 13):  # six records last through position 12
            assert kinds[:n].count("D") <= -(-n // 2), kinds[:n]

    def test_records_keep_their_own_order(self):
        fused = _rrf_fuse_summary_lists(_lists(), cap=20, derived_share=0.5)
        assert [i["record_id"] for i in fused if i["record_id"].startswith("vec")] == [f"vec{i}" for i in range(6)]

    def test_derived_keep_their_own_order(self):
        plain = _rrf_fuse_summary_lists(_lists(), cap=40)
        bounded = _rrf_fuse_summary_lists(_lists(), cap=40, derived_share=0.5)
        order = [i["record_id"] for i in plain if not i["record_id"].startswith("vec")]
        got = [i["record_id"] for i in bounded if not i["record_id"].startswith("vec")]
        assert got == order[: len(got)]

    def test_no_records_means_derived_fill_everything(self):
        # Not a balance: when the best evidence is derived, it fills the list.
        fused = _rrf_fuse_summary_lists(_lists(n_vector=0), cap=20, derived_share=0.5)
        assert len(fused) == 20 and _kinds(fused).count("D") == 20

    def test_top_item_may_be_derived(self):
        fused = _rrf_fuse_summary_lists(_lists(), cap=20, derived_share=0.5)
        assert _kinds(fused)[0] == "D"

    def test_context_only_records_are_not_promoted(self):
        lists = _lists(n_vector=0) + [("recent", 1.0, [_item(f"vec{i}", "recent") for i in range(5)])]
        fused = _rrf_fuse_summary_lists(lists, cap=20, derived_share=0.5, context_sources=frozenset({"recent"}))
        assert _kinds(fused)[:20].count("R") == 0

    def test_share_one_is_the_plain_fusion(self):
        plain = _rrf_fuse_summary_lists(_lists(), cap=20)
        same = _rrf_fuse_summary_lists(_lists(), cap=20, derived_share=1.0)
        assert [i["record_id"] for i in plain] == [i["record_id"] for i in same]

    def test_a_record_also_seen_by_a_derived_lane_counts_as_a_record(self):
        # A weak vector sighting: by score alone the item sits below every stat.
        lists = _lists(n_vector=0) + [("vector", 0.1, [_item("fact29", "vector")])]
        assert "fact29" not in [i["record_id"] for i in _rrf_fuse_summary_lists(lists, cap=4)]
        fused = _rrf_fuse_summary_lists(lists, cap=4, derived_share=0.5)
        assert "fact29" in [i["record_id"] for i in fused[:2]]

    def test_a_mixed_item_does_not_spend_the_derived_budget(self):
        mixed = _item("mixed", "fact")
        lists = _lists(n_vector=0) + [("vector", 1.0, [_item("mixed", "vector"), _item("vec1", "vector")])]
        lists[1] = ("facts_store", 1.5, [mixed] + lists[1][2])
        fused = _rrf_fuse_summary_lists(lists, cap=4, derived_share=0.5)
        assert [i["record_id"] for i in fused] == ["mixed", "stat0", "stat1", "vec1"]

    def test_pins_do_not_undo_the_bound(self):
        lists = _lists() + [("goals", 1.0, [_item(f"goal{i}", "user_goal") for i in range(3)])]
        fused = _rrf_fuse_summary_lists(lists, cap=20, derived_share=0.5, min_per_source={"goals": 2})
        kinds = ["R" if i["record_id"].startswith("vec") else "D" for i in fused]
        assert kinds.count("R") == 6
        assert sum(1 for i in fused if i["record_id"].startswith("goal")) >= 2
        assert kinds[:12].count("R") == 6

    def test_bound_selection_helper(self):
        ranked = ["d1", "d2", "d3", "r1", "d4", "r2", "c1"]
        out = es.bounded_derived_selection(
            ranked,
            is_derived=lambda k: k.startswith("d"),
            is_record=lambda k: k.startswith("r"),
            share=0.5,
            cap=6,
        )
        assert out == ["d1", "r1", "d2", "r2", "d3", "d4"]


# ---------------------------------------------------------------------------
# End to end: write-through-read on a migrated database
# ---------------------------------------------------------------------------

_VOCAB = ("who", "close", "circle", "friend", "my")


class _BagOfWordsEncoder:
    def run_inference(self, payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        texts = payload.get("texts")
        if texts is None:
            single = payload.get("text") or payload.get("content") or ""
            texts = [single] if single else []
        return {"vectors": [self._vector(str(t)) for t in texts], "dims": len(_VOCAB),
                "model": config.get("model") or "fake", "provider": "fake", "normalized": True}

    @staticmethod
    def _vector(text: str) -> List[float]:
        words = set(text.lower().split())
        raw = [1.0 if w in words else 0.0 for w in _VOCAB]
        norm = sum(v * v for v in raw) ** 0.5 or 1.0
        return [v / norm for v in raw]


#: Filler words that are not in the encoder's vocabulary, so every message is distinct
#: text (no near-duplicates) at the same cosine.
_FILLER = [f"w{i:03d}" for i in range(400)]


class TestRetrievalPath:
    @pytest.fixture()
    def node(self, tmp_path, monkeypatch, pin_db_path):
        from topos.storage.adapters.sqlite.stores import SQLiteVectorIndex
        from topos.storage.db.migrations import apply_all_migrations

        db_path = tmp_path / "shaping.db"
        conn = sqlite3.connect(str(db_path))
        apply_all_migrations(conn)
        pin_db_path(db_path)
        monkeypatch.setattr("topos.core.state.get_db_connection", lambda *a, **k: conn)
        monkeypatch.setattr("topos.engine.backends.huggingface.HuggingFaceAdapter", _BagOfWordsEncoder)
        monkeypatch.setattr("topos.features.signal.query_embed_cache.get_cached_query_embedding", lambda *a, **k: None)
        monkeypatch.setattr("topos.features.signal.query_embed_cache.set_cached_query_embedding", lambda *a, **k: None)
        monkeypatch.setenv("TOPOS_RERANK", "off")
        monkeypatch.setenv("TOPOS_EMBED_MODEL", "fake")
        monkeypatch.setenv("TOPOS_EVIDENCE_FLOOR_MARGIN", "0.2")
        index = SQLiteVectorIndex(conn)
        n = 0

        def add(record_id: str, conversation_id: str, words: str) -> None:
            nonlocal n
            text = f"{words} " + " ".join(_FILLER[n * 6:(n + 1) * 6])
            n += 1
            index.upsert(
                {"record_id": record_id, "source_id": "imessage", "signal_dimension": "relationships",
                 "model": "fake", "dims": len(_VOCAB), "text_preview": text, "search_text": text,
                 "record_type": "conversation_message", "chunk_index": 0,
                 "conversation_id": conversation_id},
                vector=_BagOfWordsEncoder._vector(text),
            )

        for i in range(30):
            add(f"long:{i}", "conv-long", "who close circle")
        for i in range(6):
            add(f"other:{i}", f"conv-{i}", "who close circle friend")
        yield conn
        conn.close()

    def _hits(self, conn) -> List[str]:
        from topos.query.manifest_validation import resolve_scope_manifest
        from topos.query.retrieval import DefaultSignalRetrievalAdapter
        from topos.query.types import RetrievalRequest
        from topos.storage.adapters.factory import AdapterFactory

        bundle = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn)).retrieve(
            RetrievalRequest(
                manifest=resolve_scope_manifest("messages:read"),
                access_mode="summary",
                query_text="who is in my close circle",
                installed_source_ids=["imessage"],
                disclosure_tier="owner_raw",
            )
        )
        return [str(h.get("record_id")) for h in bundle.context_packet.get("semantic_hits") or []]

    def test_switch_off_lets_one_conversation_fill_the_lane(self, node, monkeypatch):
        monkeypatch.delenv("TOPOS_EVIDENCE_SHAPING", raising=False)
        hits = self._hits(node)
        assert len(hits) == 20
        assert sum(1 for h in hits if h.startswith("long:")) >= 14

    def test_switch_on_makes_the_long_conversation_one_witness(self, node, monkeypatch):
        monkeypatch.setenv("TOPOS_EVIDENCE_SHAPING", "on")
        hits = self._hits(node)
        assert sum(1 for h in hits if h.startswith("long:")) == 2
        assert {f"other:{i}" for i in range(6)} <= set(hits)

    def test_fusion_gets_the_share_only_when_on(self, node, monkeypatch):
        from topos.query import retrieval

        seen: List[Any] = []
        real = retrieval._rrf_fuse_summary_lists

        def spy(*args, **kwargs):
            seen.append(kwargs.get("derived_share"))
            return real(*args, **kwargs)

        monkeypatch.setattr(retrieval, "_rrf_fuse_summary_lists", spy)
        monkeypatch.delenv("TOPOS_EVIDENCE_SHAPING", raising=False)
        self._hits(node)
        monkeypatch.setenv("TOPOS_EVIDENCE_SHAPING", "on")
        monkeypatch.setenv("TOPOS_DERIVED_SHARE_MAX", "0.4")
        self._hits(node)
        assert seen == [None, 0.4]

    def test_shaped_hits_carry_no_conversation_id(self, node, monkeypatch):
        from topos.query import retrieval

        monkeypatch.setenv("TOPOS_EVIDENCE_SHAPING", "on")
        hits, _ = retrieval._semantic_hits("who is in my close circle", source_id="imessage", limit=60)
        shaped = es.shape_vector_hits(hits, conn=node, limit=20)
        assert shaped and not any("conversation_id" in h for h in shaped)

    def test_vector_fetch_limit_follows_the_switch(self, monkeypatch):
        from topos.query import retrieval

        monkeypatch.delenv("TOPOS_EVIDENCE_SHAPING", raising=False)
        assert retrieval._vector_fetch_limit() == retrieval._SEMANTIC_HIT_LIMIT
        monkeypatch.setenv("TOPOS_EVIDENCE_SHAPING", "on")
        assert retrieval._vector_fetch_limit() == vector_settings.evidence_pool_limit()
