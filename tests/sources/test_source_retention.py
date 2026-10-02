"""Keep a source's data only from a date onward: the retention floor and its removal.

The owner keeps iMessage only from 2026-01-01. The floor must (1) remove every stored
row older than it with everything derived from it, (2) keep what newer rows still
support, trimmed, (3) leave the owner-attested provenance ledger and the owner's own
decisions alone, (4) stop every write door and every sync mode from bringing the old
rows back, and (5) do all of it through a dry run first, on the owner socket only.

Every database here is synthetic and lives in ``tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import topos.sources.retention as retention
from topos.sources.retention import (
    KEPT_REFERENCE_TABLES,
    RetentionError,
    apply_retention,
    clear_retention_floor,
    compact_database,
    compaction_status,
    is_below_floor,
    normalize_keep_since,
    plan_retention,
    retention_floor,
)

DS = "owner:topos:retention"
FLOOR = "2026-01-01"
NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

#: message id -> (conversation, event time). 104 sits exactly on the floor and is kept.
MESSAGES = {
    "imessage:101": ("c-old", "2024-03-05T10:00:00+00:00"),
    "imessage:102": ("c-old", "2025-12-31T23:59:59+00:00"),
    "imessage:103": ("c-mix", "2025-06-01T08:00:00+00:00"),
    "imessage:104": ("c-mix", "2026-01-01T00:00:00+00:00"),
    "imessage:105": ("c-mix", "2026-09-20T09:00:00+00:00"),
    "imessage:106": ("c-mix", "2025-02-02T12:00:00+00:00"),
    "imessage:107": ("c-mix", "2025-03-03T12:00:00+00:00"),
    # Later than the floor as text, earlier as an instant (2025-12-31T22:00Z): removed.
    "imessage:110": ("c-mix", "2026-01-01T03:00:00+05:00"),
}
OLD = {"imessage:101", "imessage:102", "imessage:103", "imessage:107", "imessage:110"}
ATTESTED = "imessage:106"
KEPT = {"imessage:104", "imessage:105", ATTESTED}
OTHER_SOURCE = "signal:201"

_PROVENANCE_DDL = ("CREATE TABLE IF NOT EXISTS ingest_provenance_records (message_id TEXT PRIMARY KEY, "
                   "enrollment_id TEXT NOT NULL, enrollment_revision INTEGER NOT NULL, job_id TEXT NOT NULL, "
                   "row_identity TEXT NOT NULL)")


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import topos.ingestion.local_sync as local_sync

    monkeypatch.setenv("IMESSAGE_CHAT_DB", str(tmp_path / "no-default-chat.db"))
    monkeypatch.setattr(local_sync, "_run_local_sync_enrichment_if_enabled", lambda **_kw: None)


def _staging(message_id: str, conversation: str, when: str, *, source: str = "imessage") -> dict:
    return {"message_id": message_id, "dataset_id": DS, "thread_id": conversation, "ts": when,
            "sender_type": "human", "sender_id": f"handle-{conversation}", "from_self": False,
            "content": f"words of {message_id}", "source_id": source}


def build_node(path: Path) -> sqlite3.Connection:
    """A node with the floor's whole cascade populated, old and new rows side by side."""
    from topos.analytics.messenger_communities import ensure_messenger_analytics_tables
    from topos.storage.canonical import ConversationsTablesManager
    from topos.storage.db.migrations import apply_all_migrations
    from topos.storage.raw.raw_tables_manager import RawTablesManager

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    manager = ConversationsTablesManager(conn)
    manager.ensure_tables()
    ensure_messenger_analytics_tables(conn)
    manager.upsert_message_batch([_staging(mid, conv, when) for mid, (conv, when) in MESSAGES.items()], DS, "imessage")
    manager.upsert_message_batch([_staging(OTHER_SOURCE, "c-signal", "2024-01-01T00:00:00+00:00", source="signal")],
                                 DS, "signal")
    raw = RawTablesManager(conn)
    for mid, (conv, when) in MESSAGES.items():
        raw.write_raw_record(source_id="imessage", source_record_id=mid, payload={"id": mid}, source_type="chat_messages")
    for mid, (conv, when) in {**MESSAGES, OTHER_SOURCE: ("c-signal", "2024-01-01T00:00:00+00:00")}.items():
        source = "signal" if mid == OTHER_SOURCE else "imessage"
        conn.execute("INSERT INTO timeline (event_at, record_id, source_id, canonical_table, record_type) "
                     "VALUES (?,?,?, 'conversation_messages', 'message')", (when, mid, source))
        conn.execute("INSERT INTO enrichment_record_progress (source_id, job_id, record_id) VALUES (?, 'entities', ?)",
                     (source, mid))
    for mid in ("imessage:101", "imessage:105", OTHER_SOURCE):
        conn.execute("INSERT INTO signal_embeddings (embedding_id, record_id, source_id, signal_dimension, model, "
                     "provider, dims, text_preview, provenance_json) VALUES (?,?,?, 'memory','test','test', 4, 'x', '{}')",
                     (f"emb-{mid}", mid, "signal" if mid == OTHER_SOURCE else "imessage"))
    for entity_id, name in (("ent_old", "Old Friend"), ("ent_both", "Steady Friend")):
        conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, mention_count) "
                     "VALUES (?, 'person', ?, ?, 1)", (entity_id, name, name.lower()))
    for mention_id, entity_id, mid in (("m1", "ent_old", "imessage:101"), ("m2", "ent_both", "imessage:103"),
                                       ("m3", "ent_both", "imessage:105")):
        conn.execute("INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, canonical_table, "
                     "surface_text, event_at) VALUES (?,?,?, 'imessage', 'conversation_messages', 'name', ?)",
                     (mention_id, entity_id, mid, MESSAGES[mid][1]))
    conn.execute("INSERT INTO message_entities (entity_id, record_id, message_id, source_id, entity_text, payload_json) "
                 "VALUES ('me-1', 'imessage:101', 'imessage:101', 'imessage', 'x', '{}')")
    conn.execute("INSERT INTO message_emotions (emotion_id, record_id, source_id, payload_json) "
                 "VALUES ('em-1', 'imessage:102', 'imessage', '{}')")
    conn.execute("INSERT INTO entity_review (review_id, surface_text, candidate_entity_id, score, record_id) "
                 "VALUES ('rv-1', 'x', 'ent_both', 0.5, 'imessage:103')")
    conn.execute("INSERT INTO stat_seen (stat_id, record_id, seen_at) VALUES ('s1', 'imessage:107', '2026-01-02')")
    ref = lambda mid: {"table": "conversation_messages", "id": mid}  # noqa: E731
    objects = {
        "obj-old": [ref("imessage:101"), ref("imessage:102")],
        "obj-mix": [ref("imessage:103"), ref("imessage:105")],
        "obj-other": [ref(OTHER_SOURCE)],
    }
    for object_id, refs in objects.items():
        payload = {"evidence": [{"record_id": r["id"], "text": f"quote of {r['id']}"} for r in refs]}
        conn.execute("INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, "
                     "confidence, source_refs_json, valid_from, extractor_version, created_by, updated_by, created_at, "
                     "updated_at) VALUES (?, 'relationships', 'person_reading', ?, ?, 0.5, ?, '2026-01-01', 't', 't', 't', "
                     "'2026-01-02', '2026-01-02')",
                     (object_id, object_id, json.dumps(payload), json.dumps(refs)))
    conn.execute("INSERT INTO extraction_artifacts (artifact_id, artifact_type, payload_json, dimension_affinity_json, "
                 "source_refs_json, source_ref_hash, confidence, extracted_at, extractor_version) "
                 "VALUES ('art-old', 'claim', '{}', '{}', ?, 'h', 0.5, '2026-01-02', 't')",
                 (json.dumps([ref("imessage:102")]),))
    for conversation in ("c-old", "c-mix"):
        conn.execute("INSERT INTO graph_nodes (node_id, node_type, label, metadata_json, source_id) "
                     "VALUES (?, 'conversation', 'c', '{}', 'imessage')", (f"conversation:{conversation}",))
        conn.execute("INSERT INTO graph_nodes (node_id, node_type, label, metadata_json, source_id) "
                     "VALUES (?, 'contact', 'h', '{}', 'imessage')", (f"contact:handle-{conversation}",))
        conn.execute("INSERT INTO graph_edges (edge_id, src_node_id, dst_node_id, edge_type, weight, metadata_json, "
                     "source_id) VALUES (?,?,?, 'message_frequency', 1, '{}', 'imessage')",
                     (f"edge-{conversation}", f"contact:handle-{conversation}", f"conversation:{conversation}"))
    conn.execute("INSERT INTO messenger_social_edges (dataset_id, period_key, source_scope, source_id, target_id, "
                 "weight, created_at, updated_at) VALUES (?, '2024-03', 'imessage', 'a', 'b', 1, 'x', 'x')", (DS,))
    conn.execute(_PROVENANCE_DDL)
    conn.execute("INSERT INTO ingest_provenance_records VALUES (?, 'enr-1', 1, 'job-1', 'identity')", (ATTESTED,))
    conn.execute("INSERT INTO owner_only_records (canonical_table, record_id) VALUES ('conversation_messages', 'imessage:107')")
    conn.commit()
    return conn


@pytest.fixture
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = build_node(tmp_path / "node.db")
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: conn)
    yield conn
    conn.close()


def _ids(conn: sqlite3.Connection, table: str, column: str = "message_id") -> set:
    return {str(r[0]) for r in conn.execute(f"SELECT {column} FROM {table}")}


def _dump(conn: sqlite3.Connection) -> dict:
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name"):
        out[name] = sorted(json.dumps([str(v) for v in row]) for row in conn.execute(f'SELECT * FROM "{name}"'))
    return out


def _residue(conn: sqlite3.Connection, removed: set) -> dict:
    """Every row in any table naming a removed id by record_id/message_id, minus the kept list."""
    found = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'"):
        if name in KEPT_REFERENCE_TABLES:
            continue
        columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
        for column in ("record_id", "message_id", "source_record_id"):
            if column in columns:
                hits = sum(1 for (v,) in conn.execute(f'SELECT "{column}" FROM "{name}"') if v in removed)
                if hits:
                    found[f"{name}.{column}"] = hits
    return found


def _apply(conn, **kwargs):
    return apply_retention(conn, "imessage", FLOOR, now=NOW, **kwargs)


# --- the floor ----------------------------------------------------------------


def test_the_floor_is_an_instant_and_never_in_the_future():
    assert normalize_keep_since("2026-01-01", now=NOW) == "2026-01-01T00:00:00+00:00"
    assert normalize_keep_since("2026-01-01T05:00:00-05:00", now=NOW) == "2026-01-01T10:00:00+00:00"
    for bad in ("", "soon", "2026-13-01", None, 20260101):
        with pytest.raises(RetentionError) as refused:
            normalize_keep_since(bad, now=NOW)
        assert refused.value.code == "keep_since_invalid"
    with pytest.raises(RetentionError) as refused:
        normalize_keep_since("2026-10-02", now=NOW)
    assert refused.value.code == "keep_since_in_future"


def test_below_means_strictly_older_and_undated_is_never_below():
    assert is_below_floor("2025-12-31T23:59:59+00:00", "2026-01-01T00:00:00+00:00")
    assert not is_below_floor("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
    # An offset is compared as an instant, not as text.
    assert not is_below_floor("2025-12-31T20:00:00-05:00", "2026-01-01T00:00:00+00:00")
    assert is_below_floor("2026-01-01T03:00:00+05:00", "2026-01-01T00:00:00+00:00")
    for undated in (None, "", "yesterday"):
        assert not is_below_floor(undated, "2026-01-01T00:00:00+00:00")


def test_only_supported_sources_on_sqlite(node):
    for source in ("signal", "chatgpt", "", "imessage; drop"):
        with pytest.raises(RetentionError) as refused:
            plan_retention(node, source, FLOOR, now=NOW)
        assert refused.value.code == "retention_source_unsupported"


# --- the dry run --------------------------------------------------------------


def test_the_dry_run_counts_everything_and_writes_nothing(node):
    # A kept row inside the grant window whose exact text only an old row shares.
    node.execute("UPDATE conversation_messages SET content='words of imessage:101' WHERE message_id='imessage:105'")
    node.commit()
    before = _dump(node)
    plan = plan_retention(node, "imessage", FLOOR, now=NOW, window_days=90)
    assert _dump(node) == before
    assert retention.TABLE not in before
    assert plan["dry_run"] is True and plan["keep_since"] == "2026-01-01T00:00:00+00:00"
    assert plan["rows"]["below_floor"] == len(OLD) + 1
    assert plan["rows"]["to_remove"] == len(OLD)
    assert plan["rows"]["kept_attested"] == 1
    assert plan["rows"]["remaining"] == len(KEPT)
    tables = plan["tables"]
    assert tables["conversation_messages"] == len(OLD)
    assert tables["raw_chat_messages_imessage"] == len(OLD)
    assert tables["timeline"] == len(OLD)
    assert tables["enrichment_record_progress"] == len(OLD)
    assert tables["signal_embeddings"] == 1 and tables["vector_index"] == 1
    assert tables["entity_mentions"] == 2
    assert tables["message_entities"] == 1 and tables["message_emotions"] == 1
    assert tables["entity_review"] == 1 and tables["stat_seen"] == 1
    assert "owner_only_records" not in tables and "ingest_provenance_records" not in tables
    derived = plan["derived"]
    assert derived["signal_objects_deleted"] == 1 and derived["signal_objects_trimmed"] == 1
    assert derived["payload_evidence_items_removed"] == 1
    assert derived["extraction_artifacts_deleted"] == 1
    assert derived["conversations_emptied"] == 1
    assert derived["entities_losing_every_mention"] == 1
    assert derived["stats_refold"] is True
    assert derived["messenger_period_rows_dropped"] == 1
    window = plan["grant_window"]
    assert window["removed_inside_window"] == 0
    assert (window["kept_rows_copy_count_changes"], window["kept_rows_become_unique"]) == (1, 1)
    # 105's two nearest earlier messages are 104 and the offset row 110, which goes.
    assert window["kept_rows_review_context_changes"] == 1 and window["index_rebuild"] is True
    assert plan["bytes"]["database_file_bytes"] > 0
    text = json.dumps(plan)
    assert "words of" not in text and "quote of" not in text and "imessage:1" not in text


# --- the removal --------------------------------------------------------------


def test_apply_removes_old_rows_and_everything_derived_from_them(node):
    report = _apply(node, batch_size=50)
    assert report["state"] == "done" and report["pending"] == {}
    assert report["rows_removed"] == len(OLD) and report["kept_attested"] == 1
    assert _ids(node, "conversation_messages") == KEPT | {OTHER_SOURCE}
    assert _residue(node, OLD) == {}
    assert _ids(node, "timeline", "record_id") == KEPT | {OTHER_SOURCE}
    assert _ids(node, "raw_chat_messages_imessage", "source_record_id") == KEPT
    assert _ids(node, "signal_embeddings", "embedding_id") == {"emb-imessage:105", f"emb-{OTHER_SOURCE}"}

    # Derived objects: evidence only from removed rows goes; mixed evidence stays, trimmed,
    # and the quoted text of the removed row leaves the payload with its ref.
    objects = {r[0]: (json.loads(r[1]), json.loads(r[2])) for r in node.execute(
        "SELECT object_id, source_refs_json, payload_json FROM signal_objects")}
    assert set(objects) == {"obj-mix", "obj-other"}
    refs, payload = objects["obj-mix"]
    assert [r["id"] for r in refs] == ["imessage:105"]
    assert [e["record_id"] for e in payload["evidence"]] == ["imessage:105"]
    assert "quote of imessage:103" not in json.dumps(payload)
    assert _ids(node, "extraction_artifacts", "artifact_id") == set()

    # An entity only the removed rows mentioned goes; one newer rows mention stays, recounted.
    entities = {r[0]: r[1] for r in node.execute("SELECT entity_id, mention_count FROM entities")}
    assert "ent_old" not in entities and entities["ent_both"] == 1

    # The emptied conversation goes with its participants and graph projection; the mixed one stays.
    assert _ids(node, "conversations", "conversation_id") >= {"c-mix"}
    assert "c-old" not in _ids(node, "conversations", "conversation_id")
    assert "c-old" not in _ids(node, "conversation_participants", "conversation_id")
    nodes = _ids(node, "graph_nodes", "node_id")
    assert "conversation:c-old" not in nodes and "contact:handle-c-old" not in nodes
    assert {"conversation:c-mix", "contact:handle-c-mix"} <= nodes
    assert _ids(node, "graph_edges", "edge_id") == {"edge-c-mix"}

    # Messenger periods before the floor are gone for the source's scope.
    assert node.execute("SELECT count(*) FROM messenger_social_edges WHERE period_key < '2026-01'").fetchone()[0] == 0


def test_the_provenance_ledger_and_owner_decisions_are_never_touched(node):
    _apply(node)
    assert ATTESTED in _ids(node, "conversation_messages")
    assert _ids(node, "ingest_provenance_records") == {ATTESTED}
    assert _ids(node, "owner_only_records", "record_id") == {"imessage:107"}


def test_other_sources_and_rows_on_the_floor_are_kept(node):
    _apply(node)
    assert "imessage:104" in _ids(node, "conversation_messages")
    assert OTHER_SOURCE in _ids(node, "conversation_messages")
    assert OTHER_SOURCE in _ids(node, "timeline", "record_id")
    assert "obj-other" in _ids(node, "signal_objects", "object_id")


def test_the_floor_is_persisted_and_a_rerun_removes_nothing(node):
    _apply(node)
    assert retention_floor(node, "imessage") == "2026-01-01T00:00:00+00:00"
    before = _dump(node)
    again = _apply(node)
    assert again["rows_removed"] == 0 and again["batches"] == 0 and again["state"] == "done"
    after = _dump(node)
    after.pop(retention.TABLE), before.pop(retention.TABLE)
    assert after == before


def test_an_interrupted_batch_rolls_back_whole_and_the_rerun_finishes(node, monkeypatch):
    real = retention._drop_emptied_conversations
    calls = {"n": 0}

    def fail_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("power cut")
        return real(*args, **kwargs)

    monkeypatch.setattr(retention, "_drop_emptied_conversations", fail_second)
    monkeypatch.setattr(retention, "MIN_BATCH_SIZE", 1)
    with pytest.raises(RuntimeError):
        _apply(node, batch_size=2)
    # Batch one committed whole; batch two left no trace, not even its derived rows.
    remaining_old = OLD & _ids(node, "conversation_messages")
    assert len(remaining_old) == len(OLD) - 2
    assert remaining_old <= _ids(node, "timeline", "record_id")
    assert remaining_old <= _ids(node, "raw_chat_messages_imessage", "source_record_id")
    # The floor was persisted before anything went, and says the removal is unfinished.
    floor = node.execute(f"SELECT keep_since, state FROM {retention.TABLE}").fetchone()
    assert tuple(floor) == ("2026-01-01T00:00:00+00:00", "removing")

    monkeypatch.setattr(retention, "_drop_emptied_conversations", real)
    report = _apply(node, batch_size=2)
    assert report["state"] == "done" and report["rows_removed"] == len(remaining_old)
    assert _residue(node, OLD) == {}
    entities = {r[0] for r in node.execute("SELECT entity_id FROM entities")}
    assert "ent_old" not in entities


def test_a_failed_recompute_stays_due_and_the_next_run_retries_it(node, monkeypatch):
    from topos.features.lifecycle import derived_scrub

    real = derived_scrub.refold_statistics
    monkeypatch.setattr(derived_scrub, "refold_statistics", lambda conn: (_ for _ in ()).throw(RuntimeError("busy")))
    first = _apply(node)
    assert first["state"] == "removing" and first["pending"].get("stats") is True
    monkeypatch.setattr(derived_scrub, "refold_statistics", real)
    second = _apply(node)
    assert second["state"] == "done" and second["pending"] == {} and "stats" in second["recompute"]


def test_apply_refuses_while_a_sync_of_the_source_runs(node):
    from topos.ingestion.local_sync import exclusive_sync

    with exclusive_sync("imessage", DS) as held:
        assert held
        with pytest.raises(RetentionError) as refused:
            _apply(node)
    assert refused.value.code == "retention_sync_in_progress"
    assert OLD <= _ids(node, "conversation_messages")


def test_lifting_the_floor_brings_nothing_back(node):
    _apply(node)
    assert clear_retention_floor(node, "imessage") is True
    assert retention_floor(node, "imessage") is None
    assert not (OLD & _ids(node, "conversation_messages"))


# --- no re-import ---------------------------------------------------------------


def test_every_write_door_refuses_rows_below_the_floor(node):
    from topos.storage.canonical import ConversationsTablesManager
    from topos.storage.canonical.canonical_store import REFUSED_RETENTION_FLOOR, SQLiteCanonicalStore

    _apply(node)
    refused: dict = {}
    result = ConversationsTablesManager(node).upsert_message_batch(
        [_staging("imessage:101", "c-old", MESSAGES["imessage:101"][1]),
         _staging("imessage:108", "c-mix", "2026-09-30T10:00:00+00:00")],
        DS, "imessage", refused=refused)
    assert refused == {"imessage:101": REFUSED_RETENTION_FLOOR}
    assert result["retention_skipped"] == 1 and result["messages_created"] == 1
    assert "imessage:101" not in _ids(node, "conversation_messages")
    # No parent was made for the refused row either.
    assert "c-old" not in _ids(node, "conversations", "conversation_id")
    # The canonical writer itself refuses, whichever door calls it.
    ref = SQLiteCanonicalStore(node).upsert("conversation_messages", {
        "message_id": "imessage:102", "conversation_id": "c-old", "dataset_id": DS, "source_id": "imessage",
        "event_at": MESSAGES["imessage:102"][1], "content": "x"})
    assert ref.refused == REFUSED_RETENTION_FLOOR and not ref.created
    assert "imessage:102" not in _ids(node, "conversation_messages")
    # A filled-in time is not a native one: the writer's own fill is never below a floor.
    filled = _staging("imessage:109", "c-mix", "2020-01-01T00:00:00+00:00")
    filled["_event_time_substituted"] = True
    ConversationsTablesManager(node).upsert_message_batch([filled], DS, "imessage")
    assert "imessage:109" in _ids(node, "conversation_messages")


def _chat_db(path: Path) -> None:
    from tests.sources.test_imessage_spam_filter import _add_message, _make_chat_db, mac_ns

    chat = _make_chat_db(path)
    for rowid, when in ((1, "2019-05-05T10:00:00"), (2, "2025-12-31T23:00:00"), (3, "2026-02-02T10:00:00"),
                        (4, "2026-09-29T08:00:00")):
        stamp = datetime.fromisoformat(when).replace(tzinfo=timezone.utc).timestamp()
        _add_message(chat, rowid=rowid, chat_id=1, handle_id=1, text=f"m{rowid}", date=mac_ns(stamp))
    chat.close()


@pytest.mark.parametrize("options", [
    {"mode": "full_history"},
    {"mode": "custom", "start_date": "2015-01-01"},
    {"mode": "since_last", "confirm_start_rowid": 0},
])
def test_no_sync_mode_reimports_rows_below_the_floor(tmp_path, options):
    from topos.ingestion.local_sync import run_imessage_sync

    chat = tmp_path / "chat.db"
    _chat_db(chat)
    topos = sqlite3.connect(":memory:")
    retention.set_retention_floor(topos, "imessage", "2026-01-01T00:00:00+00:00")
    result = run_imessage_sync(DS, db_conn=topos, chat_db_path=chat, batch_size=10, sync_options=dict(options))
    assert result["status"] == "ok", result
    imported = {"imessage:3", "imessage:4"}
    assert _ids(topos, "conversation_messages") == imported
    assert _ids(topos, "raw_chat_messages_imessage", "source_record_id") == imported
    assert result["records_below_retention_floor"] == 2
    assert result["records_processed"] == len(imported)


def test_the_sync_drops_only_rows_it_dated_older():
    from topos.ingestion.local_sync import _drop_below_floor

    floor = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    rows = [{"id": "a", "created_at": floor - 1}, {"id": "b", "created_at": floor}, {"id": "c", "created_at": None}]
    kept, below = _drop_below_floor(rows, floor)
    assert [r["id"] for r in kept] == ["b", "c"] and below == 1
    assert _drop_below_floor(rows, None) == (rows, 0)


def test_the_since_last_preview_does_not_count_rows_below_the_floor(tmp_path):
    from topos.ingestion.local_sync import run_imessage_sync

    chat = tmp_path / "chat.db"
    _chat_db(chat)
    topos = sqlite3.connect(":memory:")
    retention.set_retention_floor(topos, "imessage", "2026-01-01T00:00:00+00:00")
    result = run_imessage_sync(DS, db_conn=topos, chat_db_path=chat, batch_size=10,
                               sync_options={"mode": "since_last", "dry_run": True})
    plan = result["plan"]
    assert plan["messages"] == 4 and plan["to_import"] == 2 and plan["below_retention_floor"] == 2
    assert plan["first_at"].startswith("2026-02-02")


def test_without_a_floor_the_sync_is_unchanged(tmp_path):
    from topos.ingestion.local_sync import run_imessage_sync

    chat = tmp_path / "chat.db"
    _chat_db(chat)
    topos = sqlite3.connect(":memory:")
    result = run_imessage_sync(DS, db_conn=topos, chat_db_path=chat, batch_size=10,
                               sync_options={"mode": "full_history"})
    assert len(_ids(topos, "conversation_messages")) == 4
    assert "records_below_retention_floor" not in result


# --- compaction -----------------------------------------------------------------


def test_compaction_refuses_without_room_and_reclaims_with_it(node, monkeypatch):
    import shutil
    from collections import namedtuple

    _apply(node)
    # Free pages a compaction can reclaim, as a large removal leaves them.
    node.execute("CREATE TABLE filler (body TEXT)")
    node.executemany("INSERT INTO filler VALUES (?)", [("x" * 1024,) for _ in range(500)])
    node.commit()
    node.execute("DROP TABLE filler")
    node.commit()
    status = compaction_status(node)
    assert status["reclaimable_bytes"] > 0 and status["mode"] == "vacuum"
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage(10, 10, 1))
    refused = compact_database(node, dry_run=False)
    assert refused["can_compact"] is False and refused["refusal"] == "compaction_insufficient_free_space"
    assert "compacted" not in refused
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage(10, 1, 1 << 40))
    preview = compact_database(node)
    assert preview["dry_run"] is True and "compacted" not in preview
    done = compact_database(node, dry_run=False)
    assert done["compacted"] is True and done["database_file_bytes_after"] < done["database_file_bytes"]
    assert node.execute("PRAGMA freelist_count").fetchone()[0] == 0


# --- the owner door -------------------------------------------------------------

PATH = "/v1/sources/retention"


@pytest.fixture
def app(node):
    from fastapi import FastAPI

    from topos.api.source_retention import router

    application = FastAPI()
    application.include_router(router)
    return application


def test_a_bare_request_on_the_owner_socket_is_a_dry_run(app, node):
    from fastapi.testclient import TestClient

    from topos.uds import UDSChannelApp

    before = _dump(node)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json={"source_id": "imessage", "keep_since": FLOOR})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["dry_run"] is True and response.json()["rows"]["to_remove"] == len(OLD)
    assert _dump(node) == before


def test_the_owner_socket_runs_it_and_lists_the_floor(app, node):
    from fastapi.testclient import TestClient

    from topos.uds import UDSChannelApp

    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json={"source_id": "imessage", "keep_since": FLOOR, "dry_run": False})
        listed = client.get(PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["rows_removed"] == len(OLD)
    assert body["grant_indexes"] == {"status": "skipped", "reason": "message_search_disabled"}
    floors = listed.json()["floors"]
    assert [(f["source_id"], f["state"]) for f in floors] == [("imessage", "done")]
    assert not (OLD & _ids(node, "conversation_messages"))


@pytest.mark.parametrize("payload", [
    {"source_id": "imessage"},
    {"source_id": "imessage", "keep_since": FLOOR, "dry_run": "false"},
    {"source_id": "imessage", "keep_since": 20260101},
    {"source_id": "imessage", "keep_since": FLOOR, "drop": True},
    {"source_id": "imessage", "clear": True},
    {"source_id": "imessage", "clear": True, "dry_run": False, "keep_since": FLOOR},
    {"source_id": "imessage", "keep_since": FLOOR, "batch_size": "all"},
])
def test_an_unexpected_body_is_refused_before_anything_is_read(app, node, payload):
    from fastapi.testclient import TestClient

    from topos.uds import UDSChannelApp

    before = _dump(node)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json=payload)
    assert response.status_code == 400 and response.json()["detail"] == "source_retention_payload_invalid"
    assert _dump(node) == before


def test_a_refusal_is_a_code(app, node):
    from fastapi.testclient import TestClient

    from topos.uds import UDSChannelApp

    with TestClient(UDSChannelApp(app)) as client:
        future = client.post(PATH, json={"source_id": "imessage", "keep_since": "2999-01-01"})
        other = client.post(PATH, json={"source_id": "chatgpt", "keep_since": FLOOR})
    assert (future.status_code, future.json()["detail"]) == (400, "keep_since_in_future")
    assert (other.status_code, other.json()["detail"]) == (400, "retention_source_unsupported")


def test_tcp_and_other_principals_never_reach_the_rows(app, node):
    from fastapi.testclient import TestClient

    from topos.auth import resolve_request_principal
    from topos.principal import OWNER_APP, THIRD_PARTY, Principal

    before = _dump(node)
    with TestClient(app) as client:
        response = client.post(PATH, json={"source_id": "imessage", "keep_since": FLOOR, "dry_run": False},
                               headers={"X-Transport": "uds"})
    assert response.status_code == 401
    for principal in (None, Principal(THIRD_PARTY, "uds"), Principal(OWNER_APP, "cp_relay", acting_user="o"),
                      Principal(OWNER_APP, "local_http", acting_user="o")):
        app.dependency_overrides[resolve_request_principal] = lambda p=principal: p
        with TestClient(app) as client:
            for method, path, body in (("post", PATH, {"source_id": "imessage", "keep_since": FLOOR, "dry_run": False}),
                                       ("get", PATH, None), ("post", PATH + "/compact", {"dry_run": False})):
                response = getattr(client, method)(path, **({"json": body} if body is not None else {}))
                assert response.status_code == 403 and response.json()["detail"] == "owner_socket_required"
    assert _dump(node) == before


def test_the_node_app_serves_the_routes():
    from topos.app import app

    served = {(getattr(r, "path", None), m) for r in app.routes for m in (getattr(r, "methods", None) or ())}
    assert {(PATH, "POST"), (PATH, "GET"), (PATH + "/compact", "POST")} <= served
