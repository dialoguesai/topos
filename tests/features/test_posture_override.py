"""PLAN_PROVENANCE_SPLIT P1.4: per-connector posture OVERRIDE.

Covers the four pieces this workstream owns:
  1. storage round-trip (set/get, validation, NULL=inherit) —
     storage.source_settings;
  2. effective_posture precedence (override > registry > mixed) —
     sources.registry;
  3. record_role posture interaction matrix (ambient caps, personal keeps,
     mixed unchanged) — features.provenance.roles;
  4. the engine put/get_source_settings handlers accept + persist posture.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from topos.features.provenance.roles import (
    ROLE_AMBIENT,
    ROLE_AUTHORED,
    ROLE_ADDRESSED,
    ROLE_OBSERVED,
    record_role,
)
from topos.sources.registry import effective_posture
from topos.storage import source_settings as ss


# --------------------------------------------------------------------------- #
# 1. Storage round-trip
# --------------------------------------------------------------------------- #

@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    ss.ensure_table(c)
    yield c
    c.close()


DS = "owner-user:default"


class TestSourceSettingsPostureStorage:
    def test_ensure_table_has_posture_column(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(user_ingestion_sources)").fetchall()}
        assert "posture" in cols
        assert "exclude_spam" in cols

    def test_default_is_none_inherit(self, conn):
        # No row yet -> posture None (inherit registry default).
        settings = ss.get_source_settings(conn, DS, "chatgpt_file_ingestion")
        assert settings["posture"] is None

    def test_set_and_get_roundtrip(self, conn):
        ss.put_source_settings(conn, DS, "chatgpt_file_ingestion", posture="ambient")
        assert ss.get_source_settings(conn, DS, "chatgpt_file_ingestion")["posture"] == "ambient"

    @pytest.mark.parametrize("value", ["personal", "mixed", "ambient"])
    def test_all_valid_values_persist(self, conn, value):
        ss.put_source_settings(conn, DS, "imessage", posture=value)
        assert ss.get_source_settings(conn, DS, "imessage")["posture"] == value

    def test_posture_normalizes_case_and_whitespace(self, conn):
        ss.put_source_settings(conn, DS, "imessage", posture="  AMBIENT ")
        assert ss.get_source_settings(conn, DS, "imessage")["posture"] == "ambient"

    def test_invalid_posture_raises(self, conn):
        with pytest.raises(ValueError):
            ss.put_source_settings(conn, DS, "imessage", posture="background-noise")

    def test_null_clears_override_back_to_inherit(self, conn):
        ss.put_source_settings(conn, DS, "imessage", posture="ambient")
        assert ss.get_source_settings(conn, DS, "imessage")["posture"] == "ambient"
        ss.put_source_settings(conn, DS, "imessage", posture=None)
        assert ss.get_source_settings(conn, DS, "imessage")["posture"] is None

    def test_posture_and_enabled_independent(self, conn):
        # setting posture must not wipe enabled, and vice-versa.
        ss.put_source_settings(conn, DS, "imessage", enabled=False)
        ss.put_source_settings(conn, DS, "imessage", posture="personal")
        settings = ss.get_source_settings(conn, DS, "imessage")
        assert settings["enabled"] is False
        assert settings["posture"] == "personal"
        ss.put_source_settings(conn, DS, "imessage", enabled=True)
        settings = ss.get_source_settings(conn, DS, "imessage")
        assert settings["enabled"] is True
        assert settings["posture"] == "personal"  # untouched by the enabled update

    def test_posture_only_new_row_defaults_enabled_true(self, conn):
        ss.put_source_settings(conn, DS, "browser_visits", posture="ambient")
        settings = ss.get_source_settings(conn, DS, "browser_visits")
        assert settings["enabled"] is True
        assert settings["posture"] == "ambient"

    def test_omitting_posture_leaves_it_unchanged(self, conn):
        ss.put_source_settings(conn, DS, "imessage", posture="ambient")
        # a later enabled-only update (no posture kwarg) must not clear it.
        ss.put_source_settings(conn, DS, "imessage", enabled=False)
        assert ss.get_source_settings(conn, DS, "imessage")["posture"] == "ambient"

    def test_add_column_idempotent_on_legacy_table(self):
        # Simulate a pre-P1.4 table with no posture column, then ensure_table.
        c = sqlite3.connect(":memory:")
        c.execute(
            """CREATE TABLE user_ingestion_sources (
                   dataset_id TEXT NOT NULL, source_id TEXT NOT NULL,
                   enabled INTEGER NOT NULL DEFAULT 1, last_sync_at TEXT,
                   last_error TEXT, updated_at TEXT,
                   PRIMARY KEY (dataset_id, source_id))"""
        )
        c.commit()
        ss.ensure_table(c)
        ss.ensure_table(c)  # second call must be a no-op, not an error
        cols = {r[1] for r in c.execute("PRAGMA table_info(user_ingestion_sources)").fetchall()}
        assert "posture" in cols
        assert "exclude_spam" in cols
        ss.put_source_settings(c, DS, "imessage", posture="personal")
        assert ss.get_source_settings(c, DS, "imessage")["posture"] == "personal"
        assert ss.get_source_settings(c, DS, "imessage")["exclude_spam"] is True

    def test_exclude_spam_defaults_true(self, conn):
        settings = ss.get_source_settings(conn, DS, "imessage")
        assert settings["exclude_spam"] is True

    def test_exclude_spam_roundtrip(self, conn):
        ss.put_source_settings(conn, DS, "imessage", exclude_spam=False)
        assert ss.get_source_settings(conn, DS, "imessage")["exclude_spam"] is False
        ss.put_source_settings(conn, DS, "imessage", exclude_spam=True)
        assert ss.get_source_settings(conn, DS, "imessage")["exclude_spam"] is True

    def test_exclude_spam_independent_of_posture(self, conn):
        ss.put_source_settings(conn, DS, "imessage", posture="ambient")
        ss.put_source_settings(conn, DS, "imessage", exclude_spam=False)
        settings = ss.get_source_settings(conn, DS, "imessage")
        assert settings["posture"] == "ambient"
        assert settings["exclude_spam"] is False

    def test_add_exclude_spam_column_on_posture_only_table(self):
        c = sqlite3.connect(":memory:")
        c.execute(
            """CREATE TABLE user_ingestion_sources (
                   dataset_id TEXT NOT NULL, source_id TEXT NOT NULL,
                   enabled INTEGER NOT NULL DEFAULT 1, last_sync_at TEXT,
                   last_error TEXT, posture TEXT, updated_at TEXT,
                   PRIMARY KEY (dataset_id, source_id))"""
        )
        c.commit()
        ss.ensure_table(c)
        cols = {r[1] for r in c.execute("PRAGMA table_info(user_ingestion_sources)").fetchall()}
        assert "exclude_spam" in cols
        ss.put_source_settings(c, DS, "imessage", exclude_spam=False)
        assert ss.get_source_settings(c, DS, "imessage")["exclude_spam"] is False
        c.close()
        c.close()


# --------------------------------------------------------------------------- #
# 2. effective_posture precedence
# --------------------------------------------------------------------------- #

class TestEffectivePosturePrecedence:
    def test_registry_default_when_no_override(self, conn):
        # browser_visits registry default is 'ambient'; chatgpt is 'mixed'.
        assert effective_posture("browser_visits", DS, conn) == "ambient"
        assert effective_posture("chatgpt_file_ingestion", DS, conn) == "mixed"
        assert effective_posture("demo_journal_file", DS, conn) == "personal"

    def test_override_wins_over_registry(self, conn):
        ss.put_source_settings(conn, DS, "browser_visits", posture="personal")
        assert effective_posture("browser_visits", DS, conn) == "personal"

    def test_override_can_flip_personal_to_ambient(self, conn):
        ss.put_source_settings(conn, DS, "demo_journal_file", posture="ambient")
        assert effective_posture("demo_journal_file", DS, conn) == "ambient"

    def test_cleared_override_falls_back_to_registry(self, conn):
        ss.put_source_settings(conn, DS, "browser_visits", posture="personal")
        assert effective_posture("browser_visits", DS, conn) == "personal"
        ss.put_source_settings(conn, DS, "browser_visits", posture=None)
        assert effective_posture("browser_visits", DS, conn) == "ambient"

    def test_unknown_source_defaults_mixed(self, conn):
        assert effective_posture("no_such_source_xyz", DS, conn) == "mixed"

    def test_empty_source_id_defaults_mixed(self, conn):
        assert effective_posture("", DS, conn) == "mixed"

    def test_no_conn_uses_registry_default(self):
        # Without a conn the override step is skipped -> registry default.
        assert effective_posture("browser_visits") == "ambient"
        assert effective_posture("chatgpt_file_ingestion") == "mixed"


# --------------------------------------------------------------------------- #
# 3. record_role posture interaction matrix
# --------------------------------------------------------------------------- #

def _ai_chat(sender_type):
    return {
        "message_id": "a1",
        "sender_type": sender_type,
        "content": "text",
        "event_at": "2026-06-01T10:00:00+00:00",
    }


def _self_conv():
    return {
        "message_id": "m1",
        "conversation_id": "c1",
        "sender_type": "human",
        "sender_id": "self",
        "is_from_self": 1,
        "content": "I sent this",
    }


class TestPostureCapMatrix:
    def test_ambient_caps_authored_ai_chat_to_observed(self):
        # An 'ambient'-flagged source: even a human/owner ai_chat row can never
        # be authored (belief-grade) — it caps to observed.
        assert record_role(_ai_chat("human"), table="ai_chat_messages") == ROLE_AUTHORED
        assert (
            record_role(_ai_chat("human"), table="ai_chat_messages", posture="ambient")
            == ROLE_OBSERVED
        )

    def test_ambient_caps_self_conversation_to_observed(self):
        assert record_role(_self_conv(), table="conversation_messages") == ROLE_AUTHORED
        assert (
            record_role(_self_conv(), table="conversation_messages", posture="ambient")
            == ROLE_OBSERVED
        )

    def test_ambient_caps_addressed_to_observed(self):
        assert record_role(_ai_chat("assistant"), table="ai_chat_messages") == ROLE_ADDRESSED
        assert (
            record_role(_ai_chat("assistant"), table="ai_chat_messages", posture="ambient")
            == ROLE_OBSERVED
        )

    def test_ambient_caps_authored_by_construction_to_observed(self):
        row = {"entry_id": "j1", "content": "run", "mood_tag": "calm"}
        assert record_role(row, table="journal_entries") == ROLE_AUTHORED
        assert record_role(row, table="journal_entries", posture="ambient") == ROLE_OBSERVED

    def test_ambient_leaves_observed_and_ambient_untouched(self):
        # A non-self conversation row is already observed; an activity row is
        # already ambient. The cap must not disturb them.
        other = {
            "message_id": "m2",
            "conversation_id": "c1",
            "sender_type": "human",
            "sender_id": "+31612345678",
            "is_from_self": 0,
        }
        assert record_role(other, table="conversation_messages", posture="ambient") == ROLE_OBSERVED
        visit = {"event_id": "e1", "activity_type": "visit", "url": "https://x.test"}
        assert record_role(visit, table="activity_events", posture="ambient") == ROLE_AMBIENT

    def test_personal_keeps_computed_role(self):
        # personal never UPGRADES a lesser role — an assistant reply stays
        # addressed, a non-self message stays observed.
        assert (
            record_role(_ai_chat("assistant"), table="ai_chat_messages", posture="personal")
            == ROLE_ADDRESSED
        )
        assert record_role(_ai_chat("human"), table="ai_chat_messages", posture="personal") == ROLE_AUTHORED

    def test_mixed_is_unchanged_baseline(self):
        # mixed == the pre-P1.4 per-row behaviour for every family.
        assert record_role(_ai_chat("human"), table="ai_chat_messages", posture="mixed") == ROLE_AUTHORED
        assert (
            record_role(_ai_chat("assistant"), table="ai_chat_messages", posture="mixed")
            == ROLE_ADDRESSED
        )
        assert record_role(_self_conv(), table="conversation_messages", posture="mixed") == ROLE_AUTHORED

    def test_none_posture_equals_mixed(self):
        for row, table in (
            (_ai_chat("human"), "ai_chat_messages"),
            (_ai_chat("assistant"), "ai_chat_messages"),
            (_self_conv(), "conversation_messages"),
        ):
            assert record_role(row, table=table, posture=None) == record_role(
                row, table=table, posture="mixed"
            )


# --------------------------------------------------------------------------- #
# 4. Engine handler accepts + persists posture
# --------------------------------------------------------------------------- #

class TestPutSourceSettingsHandler:
    def _run(self, coro):
        return asyncio.run(coro)

    @pytest.fixture(autouse=True)
    def _patch_db(self, monkeypatch, conn):
        import topos.core.handlers as hub

        monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
        return conn

    def test_put_persists_posture_and_echoes_it(self, conn):
        from topos.core.handlers.sources import (
            handle_put_source_settings,
            handle_get_source_settings,
        )

        put = self._run(
            handle_put_source_settings(
                {
                    "id": "r1",
                    "payload": {
                        "source_id": "browser_visits",
                        "dataset_id": DS,
                        "posture": "personal",
                    },
                }
            )
        )
        assert put["status"] == "ok"
        assert put["payload"]["posture"] == "personal"

        got = self._run(
            handle_get_source_settings(
                {"id": "r2", "payload": {"source_id": "browser_visits", "dataset_id": DS}}
            )
        )
        assert got["payload"]["posture"] == "personal"

    def test_put_rejects_invalid_posture(self, conn):
        from topos.core.handlers.sources import handle_put_source_settings

        res = self._run(
            handle_put_source_settings(
                {
                    "id": "r3",
                    "payload": {"source_id": "browser_visits", "dataset_id": DS, "posture": "garbage"},
                }
            )
        )
        assert res["status"] == "error"

    def test_put_requires_enabled_or_posture(self, conn):
        from topos.core.handlers.sources import handle_put_source_settings

        res = self._run(
            handle_put_source_settings(
                {"id": "r4", "payload": {"source_id": "browser_visits", "dataset_id": DS}}
            )
        )
        assert res["status"] == "error"

    def test_posture_settable_on_non_local_sync_source(self, conn):
        # posture applies to ANY connector (browser is ui_stream), unlike the
        # enabled toggle which is local_sync-only.
        from topos.core.handlers.sources import handle_put_source_settings

        res = self._run(
            handle_put_source_settings(
                {
                    "id": "r5",
                    "payload": {"source_id": "browser_visits", "dataset_id": DS, "posture": "ambient"},
                }
            )
        )
        assert res["status"] == "ok"
        assert res["payload"]["posture"] == "ambient"

    def test_put_persists_exclude_spam_on_imessage(self, conn):
        from topos.core.handlers.sources import (
            handle_put_source_settings,
            handle_get_source_settings,
        )

        put = self._run(
            handle_put_source_settings(
                {
                    "id": "r6",
                    "payload": {
                        "source_id": "imessage",
                        "dataset_id": DS,
                        "exclude_spam": False,
                    },
                }
            )
        )
        assert put["status"] == "ok"
        assert put["payload"]["exclude_spam"] is False

        got = self._run(
            handle_get_source_settings(
                {"id": "r7", "payload": {"source_id": "imessage", "dataset_id": DS}}
            )
        )
        assert got["payload"]["exclude_spam"] is False

    def test_put_rejects_exclude_spam_on_non_local_sync(self, conn):
        from topos.core.handlers.sources import handle_put_source_settings

        res = self._run(
            handle_put_source_settings(
                {
                    "id": "r8",
                    "payload": {
                        "source_id": "browser_visits",
                        "dataset_id": DS,
                        "exclude_spam": False,
                    },
                }
            )
        )
        assert res["status"] == "error"


# --------------------------------------------------------------------------- #
# 5. Derivation-path wiring: an ambient override strips belief eligibility
#    (the load-bearing behavioural change — proved end-to-end through the
#    StatsEngine sent-family fold and the goal-extraction role gate).
# --------------------------------------------------------------------------- #

class TestStatsSentFamilyHonoursPostureOverride:
    @pytest.fixture()
    def stats_conn(self, tmp_path, monkeypatch):
        from topos.storage.db.migrations import apply_all_migrations
        import topos.config.settings as config_settings
        import topos.core.state as state
        import topos.features.stats.definitions as defs

        db_path = tmp_path / "stats.db"
        c = sqlite3.connect(str(db_path))
        apply_all_migrations(c)
        ss.ensure_table(c)
        # The stats posture resolver reads the global connection. Injecting
        # the handle alone is not enough: state honours an injected FILE-backed
        # handle only while the resolved settings path agrees with its cached
        # path, otherwise it evicts it in favour of the (empty) live-DB guard
        # file. Point the settings path at this DB and mark this thread as the
        # owner so the injected handle is returned as-is; reset the resolver's
        # module-level cache so overrides are seen.
        monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db_path))
        monkeypatch.setattr(
            config_settings.settings, "topos_database_path", str(db_path), raising=False
        )
        monkeypatch.setattr(state, "db_conn", c, raising=False)
        monkeypatch.setattr(state, "_db_conn_path", None, raising=False)
        monkeypatch.setattr(state, "_conn_owner_thread", threading.get_ident(), raising=False)
        defs._POSTURE_RESOLVER = None
        yield c
        defs._POSTURE_RESOLVER = None
        c.close()

    def _conv_row(self, i, source_id):
        return {
            "record_id": f"m{i}",
            "_table": "conversation_messages",
            "conversation_id": "harbor-1",
            "sender_type": "human",
            "sender_id": "self",
            "is_from_self": 1,
            "source_id": source_id,
            "dataset_id": DS,
            "event_at": f"2026-06-{(i % 27) + 1:02d}T10:00:00+00:00",
        }

    def test_ambient_override_excludes_rows_from_sent_family(self, stats_conn):
        from topos.features.stats.engine import StatsEngine

        # Flag this connector ambient: its owner-typed rows must NOT count as
        # "messages I've sent".
        ss.put_source_settings(stats_conn, DS, "imessage", posture="ambient")

        engine = StatsEngine(stats_conn)
        engine.fold_batch([self._conv_row(i, "imessage") for i in range(5)])
        total = engine.read_state("messages.volume.sent.total", group_key="self")
        assert int(total.get("n") or 0) == 0

    def test_mixed_source_still_counts_sent(self, stats_conn):
        from topos.features.stats.engine import StatsEngine

        # No override on a mixed source: owner rows count normally (regression
        # guard that the wiring didn't break the baseline).
        engine = StatsEngine(stats_conn)
        engine.fold_batch([self._conv_row(i, "imessage") for i in range(5)])
        total = engine.read_state("messages.volume.sent.total", group_key="self")
        assert int(total.get("n") or 0) == 5


class TestGoalExtractionHonoursPostureOverride:
    def test_ambient_override_blocks_goal_extraction(self, monkeypatch):
        import topos.enrichment.jobs.canonical.goal_extraction_job as goal_module
        from topos.enrichment.jobs.canonical.goal_extraction_job import GoalExtractionJob
        from types import SimpleNamespace

        # Force effective_posture to 'ambient' for this source regardless of DB.
        monkeypatch.setattr(
            goal_module,
            "make_posture_resolver",
            lambda conn=None: (lambda row: "ambient"),
        )
        called = []

        async def fake_run_engine_task(engine, *, task_id, subtype, source_id,
                                       record_ids, input_payload, provider=None, model=None):
            called.extend(record_ids)
            return SimpleNamespace(
                status="completed",
                output={"goals": [{"text": "learn the mandolin", "confidence": 0.8}], "model": "m"},
                error=None,
            )

        monkeypatch.setattr(goal_module, "run_engine_task", fake_run_engine_task)
        job = GoalExtractionJob(engine=SimpleNamespace())
        # An owner-typed ai_chat row that WOULD extract a goal under mixed —
        # the ambient posture caps it below authored, so no goal is minted.
        msgs = [{
            "message_id": "a1",
            "_table": "ai_chat_messages",
            "sender_type": "human",
            "content": "I want to learn the mandolin this year",
            "source_id": "chatgpt_file_ingestion",
            "dataset_id": DS,
        }]
        results = asyncio.run(job.enrich(msgs))
        assert results == []
        assert called == []
