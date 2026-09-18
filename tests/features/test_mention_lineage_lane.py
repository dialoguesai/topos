"""The stopped-node lane: a named database file, no node, counts only.

The repair itself is covered in ``test_mention_lineage_repair.py``; these
tests cover the door around it — what it refuses to open, what it leaves
behind, that it never migrates, that an interrupted run finishes on the next
one, and that the per-table coverage it reports is the arithmetic D8 reads.

Fixtures are synthetic files in ``tmp_path``. Nothing here opens a real
node's database.
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import pytest

from topos.features.entities import mention_lineage_lane as lane
from topos.features.entities.mention_lineage import _SpineIndex
from topos.storage.db.migrations import apply_all_migrations


def _insert(conn, table, **cols):
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})", tuple(cols.values()))


def _entity(conn, entity_id, name, etype, *, contact_id=None, aliases=(), identifiers=()):
    from topos.features.entities.resolver import normalize_name

    _insert(
        conn, "entities",
        entity_id=entity_id, entity_type=etype, canonical_name=name,
        normalized_name=normalize_name(name), aliases_json=json.dumps(list(aliases)),
        identifiers_json=json.dumps(list(identifiers)), contact_id=contact_id,
        mention_count=0, is_self=0,
    )


def _mention(conn, mention_id, entity_id, record_id, table, surface="Ada Voss"):
    _insert(
        conn, "entity_mentions",
        mention_id=mention_id, entity_id=entity_id, record_id=record_id,
        canonical_table=table, surface_text=surface, source_id="imessage",
        confidence=0.9, event_at="2026-06-01T12:00:00Z",
    )


def _extracted(conn, row_id, record_id, text, etype="PER", *, table="conversation_messages"):
    payload = {
        "record_id": record_id, "entity_text": text, "entity_type": etype,
        "confidence": 0.95, "provider": "huggingface", "source_id": "imessage",
        "event_at": "2026-06-02T09:00:00Z", "canonical_table": table,
    }
    _insert(
        conn, "message_entities",
        entity_id=row_id, record_id=record_id, source_id="imessage", entity_text=text,
        provider="huggingface", payload_json=json.dumps(payload),
    )


def _build(path: Path, *, wal: bool = True) -> Path:
    """A closed database file: 5 messages, 1 journal entry, 1 location event,
    3 unstamped mentions, 1 mis-stamped, 1 orphan, 3 extracted-unlinked rows."""
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import (
        ensure_conversation_messages_table,
        ensure_conversations_table,
    )

    c = sqlite3.connect(str(path))
    if wal:
        c.execute("PRAGMA journal_mode=WAL")
    apply_all_migrations(c)
    ensure_conversations_table(c)
    ensure_conversation_messages_table(c)
    CanonicalTablesManager(c)
    for i in range(1, 6):
        _insert(
            c, "conversation_messages",
            message_id=f"m{i}", conversation_id="conv-1", dataset_id="ds", sender_type="human",
            sender_id="+15550100", event_at="2026-06-01T12:00:00Z", content="synthetic",
            source_id="imessage",
        )
    _insert(c, "journal_entries", entry_id="tl-1", content="synthetic", source_id="grow_journal")
    _insert(c, "location_events", event_id="tl-1-loc", place_name="Mill Pond", source_id="grow_journal")
    _entity(c, "ent-ada", "Ada Voss", "person")
    _entity(c, "ent-plu", "Plurigrid", "org")
    _entity(c, "ent-pond", "Mill Pond", "place")
    # defect 1: unstamped, all resolving to conversation_messages
    _mention(c, "u1", "ent-ada", "m1", "")
    _mention(c, "u2", "ent-plu", "m1", "", surface="Plurigrid")
    _mention(c, "u3", "ent-ada", "m2", "")
    # defect 3: mis-stamped and orphaned
    _mention(c, "w1", "ent-pond", "tl-1-loc", "journal_entries", surface="Mill Pond")
    _mention(c, "w2", "ent-ada", "gone", "ai_chat_messages")
    # a correct stamp
    _mention(c, "s1", "ent-pond", "tl-1", "journal_entries", surface="Mill Pond")
    # defect 2: extracted, never linked (m3, m4 resolve; m5 does not)
    _extracted(c, "x1", "m3", "Ada Voss")
    _extracted(c, "x2", "m4", "Plurigrid", "ORG")
    _extracted(c, "x3", "m5", "Nobody Known")
    c.commit()
    c.close()
    assert lane.sidecars(path) == []
    return path


@pytest.fixture()
def db(tmp_path, monkeypatch):
    # The owner's home is somewhere this test can never reach.
    monkeypatch.setattr(lane, "owner_home_root", lambda: (tmp_path / "home" / ".topos").resolve())
    return _build(tmp_path / "copy.db")


def _writes(report):
    r = report["repair"]
    return (
        r["stamp"]["stamped"], r["restamp"]["restamped"],
        r["restamp"]["quarantined"], r["relink"]["linked"],
    )


# ---------------------------------------------------------------- coverage


def test_coverage_is_the_per_table_arithmetic(db):
    c = sqlite3.connect(str(db))
    cov = lane.lineage_coverage(c)
    c.close()
    msgs = cov["tables"]["conversation_messages"]
    assert msgs == {
        "rows": 5,
        "rows_with_mention": 2,
        "rows_with_stamped_mention": 0,
        "mentions_resolving": 3,
        "mentions_stamped_here": 0,
        "unstamped_mentions": 3,
        "extracted_rows": 3,
        "extracted_unlinked_rows": 3,
    }
    assert cov["tables"]["journal_entries"]["rows_with_stamped_mention"] == 1
    assert cov["tables"]["location_events"]["mentions_resolving"] == 1
    assert cov["tables"]["location_events"]["mentions_stamped_here"] == 0
    assert cov["mentions_total"] == 6
    assert cov["mentions_unstamped_total"] == 3


# ------------------------------------------------------------------ the run


def test_a_dry_run_counts_and_leaves_the_file_byte_identical(db):
    before = lane.file_sha256(db)
    report = lane.run_stopped_node_lane(db, dry_run=True, hash_file=True)
    assert _writes(report) == (3, 1, 1, 2)
    assert report["repair"]["relink"]["unresolved"] == 1
    assert "coverage_after" not in report
    assert report["sha256_before"] == report["sha256_after"] == before
    assert lane.sidecars(db) == []


def test_the_run_closes_the_gap_it_can_and_counts_what_it_cannot(db):
    report = lane.run_stopped_node_lane(db)
    assert _writes(report) == (3, 1, 1, 2)
    after = report["coverage_after"]["tables"]["conversation_messages"]
    assert after["unstamped_mentions"] == 0
    assert after["rows_with_stamped_mention"] == 4
    assert after["extracted_unlinked_rows"] == 1, "no entity for the surface: left unlinked, not minted"
    assert report["coverage_after"]["tables"]["location_events"]["mentions_stamped_here"] == 1
    assert report["quick_check_ok"] is True
    assert lane.sidecars(db) == [], "the lane checkpoints and closes clean"


def test_a_second_run_writes_nothing(db):
    lane.run_stopped_node_lane(db)
    second = lane.run_stopped_node_lane(db)
    assert _writes(second) == (0, 0, 0, 0)
    assert second["coverage_before"] == second["coverage_after"]


def test_the_lane_never_moves_user_version(db):
    c = sqlite3.connect(str(db))
    version = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()
    report = lane.run_stopped_node_lane(db)
    assert report["user_version"] == version
    c = sqlite3.connect(str(db))
    assert c.execute("PRAGMA user_version").fetchone()[0] == version
    c.close()


def test_a_repair_that_moved_user_version_fails_the_lane(db, monkeypatch):
    real = lane.repair_mention_lineage

    def migrating(conn, **kw):
        conn.execute("PRAGMA user_version = 9999")
        return real(conn, **kw)

    monkeypatch.setattr(lane, "repair_mention_lineage", migrating)
    with pytest.raises(lane.StoppedNodeLaneError, match="user_version"):
        lane.run_stopped_node_lane(db)


def test_an_interrupted_run_finishes_on_the_next_one(db, tmp_path, monkeypatch):
    from topos.features.entities.resolver import EntityResolver

    twin = _build(tmp_path / "twin.db")
    uninterrupted = lane.run_stopped_node_lane(twin)

    real = EntityResolver.record_mention
    calls = {"n": 0}

    def crash_on_second(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("power cut")
        return real(self, *a, **kw)

    monkeypatch.setattr(EntityResolver, "record_mention", crash_on_second)
    with pytest.raises(RuntimeError, match="power cut"):
        lane.run_stopped_node_lane(db, batch_size=1)
    monkeypatch.setattr(EntityResolver, "record_mention", real)
    for suffix in lane.SIDECAR_SUFFIXES:  # a crashed process's sidecars; SQLite recovers
        Path(f"{db}{suffix}").unlink(missing_ok=True)

    resumed = lane.run_stopped_node_lane(db, batch_size=1)
    assert resumed["repair"]["relink"]["linked"] == 1, "the committed chunk is not redone"
    assert resumed["repair"]["stamp"]["stamped"] == 0, "the stamp pass had finished"
    assert resumed["coverage_after"] == uninterrupted["coverage_after"]


# ---------------------------------------------------------------- refusals


def test_the_owner_home_is_refused_without_the_stopped_flag(tmp_path, monkeypatch):
    home = tmp_path / "home" / ".topos"
    home.mkdir(parents=True)
    monkeypatch.setattr(lane, "owner_home_root", lambda: home.resolve())
    target = _build(home / "database.db")
    with pytest.raises(lane.StoppedNodeLaneError, match="--node-stopped"):
        lane.run_stopped_node_lane(target, dry_run=True)
    assert lane.run_stopped_node_lane(target, dry_run=True, node_stopped=True)["dry_run"]


@pytest.mark.parametrize("suffix", lane.SIDECAR_SUFFIXES)
def test_an_open_database_is_refused(db, suffix):
    Path(f"{db}{suffix}").write_bytes(b"")
    with pytest.raises(lane.StoppedNodeLaneError, match="sidecars"):
        lane.run_stopped_node_lane(db, dry_run=True)


def test_a_missing_file_is_refused_not_created(tmp_path):
    target = tmp_path / "nothing.db"
    with pytest.raises(lane.StoppedNodeLaneError, match="not a database file"):
        lane.run_stopped_node_lane(target)
    assert not target.exists()


def test_the_report_holds_counts_only():
    lane._assert_counts_only({"a": 1, "b": {"c": 0.5, "d": [True, None, "journal_entries"]}})
    with pytest.raises(lane.StoppedNodeLaneError):
        lane._assert_counts_only({"x": "y" * 600})
    with pytest.raises(lane.StoppedNodeLaneError):
        lane._assert_counts_only({"x": b"bytes"})


# -------------------------------------------------------------------- door


def test_the_cli_writes_a_private_report(db, tmp_path, capsys):
    out = tmp_path / "report.json"
    assert lane.main(["--database", str(db), "--dry-run", "--report", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert json.loads(out.read_text()) == printed
    assert out.stat().st_mode & 0o777 == 0o600


def test_the_cli_leaves_the_topos_log_level_as_it_found_it(db, tmp_path, capsys):
    import logging

    before = logging.getLogger("topos").level
    lane.main(["--database", str(db), "--dry-run"])
    lane.main(["--database", str(tmp_path / "absent.db")])
    assert logging.getLogger("topos").level == before


def test_the_cli_refusal_is_an_exit_code(tmp_path, capsys):
    assert lane.main(["--database", str(tmp_path / "absent.db")]) == 2
    assert "refused" in json.loads(capsys.readouterr().out)


# ------------------------------------------------- the index is the scan


def test_indexed_person_lookup_answers_like_the_linear_scan(tmp_path):
    """The spine index keys persons by name and by token; the answers must be
    the linear scan's, including ties, contacts and unbinds."""
    from topos.features.entities.resolver import normalize_name

    rng = random.Random(8)
    first = ["ada", "maya", "lee", "sam", "noor", "ivo"]
    last = ["voss", "chen", "park", "lee", "okafor"]
    c = sqlite3.connect(str(tmp_path / "idx.db"))
    apply_all_migrations(c)
    people = []
    for i in range(60):
        name = f"{rng.choice(first)} {rng.choice(last)}" if rng.random() < 0.8 else rng.choice(first)
        contact = f"c{i}" if rng.random() < 0.4 else None
        _entity(c, f"ent-{i:03d}", name, "person", contact_id=contact)
        people.append((f"ent-{i:03d}", normalize_name(name), contact))
    c.commit()
    index = _SpineIndex(c)
    people.sort()

    def linear_contact(normalized):
        for eid, name, contact in people:
            if name == normalized and contact:
                return eid
        if " " in normalized:
            return None
        hit, matches = None, 0
        for eid, name, contact in people:
            if normalized in name.split():
                matches += 1
                if contact:
                    hit = eid
        return hit if matches == 1 else None

    def linear_single(normalized):
        cands = [eid for eid, name, _ in people if normalized in name.split()]
        return cands[0] if len(cands) == 1 else None

    probes = set(first) | set(last) | {f"{a} {b}" for a in first for b in last} | {"zed"}
    for probe in sorted(probes):
        assert index._contact_person(probe) == linear_contact(probe), probe
        single = index.persons_by_token.get(probe, [])
        assert (single[0][0] if len(single) == 1 else None) == linear_single(probe), probe
    c.close()


# ------------------------------------------------------ id columns are real


def test_browser_visits_resolve_by_their_record_id(tmp_path):
    """``browser_visits`` is keyed by ``record_id``; a map naming ``visit_id``
    errored per lookup and skipped the table without a word."""
    from topos.features.entities.mention_lineage import (
        CANONICAL_ID_COLUMNS,
        _present_tables,
        resolve_record_tables,
    )
    from topos.storage.raw.browser_flat_tables import ensure_browser_visits_table

    c = sqlite3.connect(str(tmp_path / "bv.db"))
    apply_all_migrations(c)
    ensure_browser_visits_table(c)
    cols = {row[1] for row in c.execute("PRAGMA table_info(browser_visits)")}
    assert CANONICAL_ID_COLUMNS["browser_visits"] in cols
    assert ("browser_visits", "record_id") in _present_tables(c)
    c.execute("INSERT INTO browser_visits (record_id, url, visited_at) VALUES ('bv-1', 'https://example.test/', '2026-06-01T00:00:00Z')")
    assert resolve_record_tables(c, "bv-1") == ["browser_visits"]
    cov = lane.lineage_coverage(c)
    assert cov["tables"]["browser_visits"]["rows"] == 1
    assert "id_column_missing" not in cov
    c.close()


# -------------------------------------------- why an extracted row is unlinked


def test_unlinked_rows_are_classified_by_the_writers_own_filters(db):
    c = sqlite3.connect(str(db))
    _insert(c, "conversation_messages", message_id="m6", conversation_id="conv-1", dataset_id="ds",
            sender_type="human", sender_id="+15550100", event_at="2026-06-01T12:00:00Z",
            content="synthetic", source_id="imessage")
    _insert(c, "conversation_messages", message_id="m7", conversation_id="conv-1", dataset_id="ds",
            sender_type="human", sender_id="+15550100", event_at="2026-06-01T12:00:00Z",
            content="synthetic", source_id="imessage")
    _extracted(c, "x6", "m6", "June 2nd", "DATE")
    payload = {"record_id": "m7", "entity_text": "Ada Voss", "entity_type": "PER",
               "confidence": 0.4, "provider": "huggingface", "canonical_table": "conversation_messages"}
    _insert(c, "message_entities", entity_id="x7", record_id="m7", source_id="imessage",
            entity_text="Ada Voss", provider="huggingface", payload_json=json.dumps(payload))
    c.commit()
    c.close()
    before = lane.run_stopped_node_lane(db, dry_run=True)["extracted_unlinked_by_reason"]
    msgs = before["tables"]["conversation_messages"]
    assert msgs["resolves_to_existing_entity"] == 2  # m3, m4: the repair's work
    assert msgs["named_but_no_existing_entity"] == 1  # m5
    assert msgs["value_types_only"] == 1  # m6
    assert msgs["named_below_confidence_floor"] == 1 and msgs["person_below_floor"] == 1  # m7
    after = lane.run_stopped_node_lane(db)["extracted_unlinked_by_reason"]
    assert after["tables"]["conversation_messages"]["resolves_to_existing_entity"] == 0
    assert after["tables"]["conversation_messages"]["named_but_no_existing_entity"] == 1
