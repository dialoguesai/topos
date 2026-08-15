"""Mega-cluster splitting + labeling from canonical (pre-redaction) text.

Live measurements behind both changes (2026-08-15, 167 clusters):
  * 23 clusters of 120+ members held 51% of all members; base-name collisions
    ("Cameron and Danielle" x13) concentrated in exactly those clusters, and
    one 147-member cluster mixed T-Mobile spam with housing logistics — no
    label can name a bag with no subject.
  * Stored previews are redacted ([NAME]/[ACCOUNT]) while the canonical row
    holds the raw text; colliding clusters carried 2.2x the redaction density.
    The labeler was naming de-specified text.
"""

from __future__ import annotations

import math
import random
import sqlite3

import pytest

from topos.features.signal import topic_clustering as tc


def _blob_members(blob: int, n: int, *, text: str, dims: int = 8) -> list[dict]:
    rng = random.Random(blob * 101 + 7)
    out = []
    for i in range(n):
        vec = [0.0] * dims
        vec[blob % dims] = 1.0
        noisy = [x + rng.uniform(-0.04, 0.04) for x in vec]
        norm = math.sqrt(sum(v * v for v in noisy))
        out.append(
            {
                "record_id": f"r{blob}_{i}",
                "source_id": "chatgpt_file_ingestion",
                "signal_dimension": "memory",
                "record_type": "ai_chat_message",
                "text_preview": text,
                "vector": [v / norm for v in noisy],
                "metadata": {},
            }
        )
    return out


class TestSplitOversized:
    def test_a_two_subject_mega_cluster_splits(self):
        members = _blob_members(0, 150, text="kayak river paddle trip") + _blob_members(
            1, 150, text="mortgage refinance lender rates"
        )
        cluster = tc._build_cluster("tc_mega", members, cluster_index=0)
        cluster["dimension"] = cluster["primary_dimension"] = "memory"
        out = tc._post_process_facet_clusters([cluster])
        assert len(out) >= 2, "two separated subjects must not stay one bag"
        assert sum(c["member_count"] for c in out) == 300, "no member may be lost"
        assert {c["dimension"] for c in out} == {"memory"}, "fragments inherit the parent facet"
        assert any(c["cluster_id"] == "tc_mega" for c in out), (
            "the parent id survives on a fragment so stable-id matching degrades gracefully"
        )

    def test_a_coherent_mega_cluster_remerges_to_one(self):
        """The similarity merge is the split's veto: one subject stays one cluster."""
        members = _blob_members(0, 300, text="kayak river paddle trip")
        cluster = tc._build_cluster("tc_one", members, cluster_index=0)
        cluster["dimension"] = cluster["primary_dimension"] = "memory"
        out = tc._post_process_facet_clusters([cluster])
        assert len(out) == 1
        assert out[0]["member_count"] == 300

    def test_split_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("TOPOS_CLUSTER_SPLIT_SIZE", "0")
        members = _blob_members(0, 150, text="kayak") + _blob_members(1, 150, text="mortgage")
        cluster = tc._build_cluster("tc_off", members, cluster_index=0)
        out = tc._split_oversized_clusters([cluster])
        assert len(out) == 1

    def test_members_without_vectors_are_never_split(self):
        """The relabel path strips vectors; splitting there would corrupt membership."""
        members = _blob_members(0, 300, text="kayak")
        for m in members:
            m.pop("vector")
        cluster = tc._build_cluster("tc_novec", members, cluster_index=0)
        out = tc._split_oversized_clusters([cluster])
        assert len(out) == 1 and out[0] is cluster


class TestRawLabelText:
    @pytest.fixture()
    def conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE conversation_messages (
                message_id TEXT PRIMARY KEY, conversation_id TEXT, dataset_id TEXT,
                sender_type TEXT, sender_id TEXT, content TEXT, event_at TEXT,
                source_id TEXT, metadata_json TEXT, created_at TEXT)"""
        )
        conn.execute(
            "INSERT INTO conversation_messages (message_id, content, source_id) VALUES (?,?,?)",
            ("imsg:1", "dinner with Jane Patterson at the convent", "imessage"),
        )
        conn.commit()
        yield conn
        conn.close()

    def _cluster(self):
        return {
            "cluster_id": "tc_x",
            "label": "t / l",
            "dimension": "relationships",
            "primary_dimension": "relationships",
            "member_count": 1,
            "label_terms": [],
            "members": [
                {
                    "record_id": "imsg:1",
                    "source_id": "imessage",
                    "record_type": "conversation_message",
                    "text_preview": "dinner with [NAME] at the [NAME]",
                    "metadata": {},
                }
            ],
            "metadata": {},
        }

    def test_hydration_attaches_the_canonical_text_in_memory(self, conn):
        cluster = self._cluster()
        n = tc._hydrate_label_texts(conn, [cluster])
        assert n == 1
        member = cluster["members"][0]
        assert "Jane Patterson" in member["label_text"]
        assert member["text_preview"] == "dinner with [NAME] at the [NAME]", (
            "the stored preview stays redacted; only the in-memory copy is raw"
        )

    def test_the_prompt_reads_the_raw_text(self, conn):
        from topos.features.signal.cluster_labels import build_contrastive_label_prompt

        cluster = self._cluster()
        tc._hydrate_label_texts(conn, [cluster])
        prompt = build_contrastive_label_prompt(cluster)
        assert "Jane Patterson" in prompt
        assert "[NAME]" not in prompt

    def test_distinguishing_terms_come_from_the_raw_text(self, conn):
        cluster = self._cluster()
        tc._hydrate_label_texts(conn, [cluster])
        counts = tc._member_term_counts(cluster["members"])
        assert "patterson" in counts
        assert "name" not in counts

    def test_kill_switch_restores_redacted_labeling(self, conn, monkeypatch):
        monkeypatch.setenv("TOPOS_CLUSTER_LABEL_RAW_TEXT", "off")
        cluster = self._cluster()
        assert tc._hydrate_label_texts(conn, [cluster]) == 0
        assert "label_text" not in cluster["members"][0]

    def test_persist_never_writes_the_raw_text(self, conn):
        """The privacy pin: redaction stays exactly where it is on disk."""
        conn.executescript(
            """CREATE TABLE topic_clusters (
                cluster_id TEXT PRIMARY KEY, label TEXT, dimension TEXT,
                member_count INTEGER, source_mix_json TEXT, label_terms_json TEXT,
                centroid_preview TEXT, model TEXT, provider TEXT, sync_batch_id TEXT,
                metadata_json TEXT, centroid_vector BLOB,
                created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE topic_cluster_members (
                member_id TEXT PRIMARY KEY, cluster_id TEXT, record_id TEXT,
                source_id TEXT, record_type TEXT, text_preview TEXT,
                weight REAL, metadata_json TEXT, created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(cluster_id, record_id, source_id));"""
        )
        cluster = self._cluster()
        tc._hydrate_label_texts(conn, [cluster])
        tc.persist_topic_clusters(conn, [cluster], commit=True)
        blob = " ".join(
            " ".join(str(v) for v in row)
            for table in ("topic_clusters", "topic_cluster_members")
            for row in conn.execute(f"SELECT * FROM {table}")
        )
        assert "Jane Patterson" not in blob
        assert "[NAME]" in blob
