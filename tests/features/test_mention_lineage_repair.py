"""The lineage repair: stamp, re-stamp or quarantine, relink — idempotently.

What it repairs, measured on a quarantined copy of a live node 2026-09-17:
17,203 unstamped mentions (all resolving to ``conversation_messages``),
98 mentions stamped ``journal_entries`` whose rows live in ``location_events``,
7 stamped ``ai_chat_messages`` whose rows exist nowhere, and 11,637 + 2,751 +
894 records extracted into ``message_entities`` and never linked into the
spine. Every rule fails toward leaving the row alone: a stamp is recovered
only when the record resolves to exactly one table, a link is written only
when the surface resolves to an EXISTING entity by the resolver's exact
tiers, and a mention that cites no row is moved to quarantine, never deleted.

Fixtures are synthetic. Nothing here opens a real node's database.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.mention_lineage import (
    QUARANTINE_TABLE,
    repair_mention_lineage,
)
from topos.storage.db.migrations import apply_all_migrations


def _insert(conn, table, **cols):
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})", tuple(cols.values()))


def _entity(conn, entity_id, name, etype, *, aliases=(), identifiers=(), contact_id=None, mentions=0):
    from topos.features.entities.resolver import normalize_name

    _insert(
        conn,
        "entities",
        entity_id=entity_id,
        entity_type=etype,
        canonical_name=name,
        normalized_name=normalize_name(name),
        aliases_json=json.dumps(list(aliases)),
        identifiers_json=json.dumps(list(identifiers)),
        contact_id=contact_id,
        mention_count=mentions,
        is_self=0,
    )


def _mention(conn, mention_id, entity_id, record_id, table, surface="Ada Voss", **extra):
    cols = dict(
        mention_id=mention_id,
        entity_id=entity_id,
        record_id=record_id,
        canonical_table=table,
        surface_text=surface,
        source_id="imessage",
        confidence=0.9,
        event_at="2026-06-01T12:00:00Z",
    )
    cols.update(extra)
    _insert(conn, "entity_mentions", **cols)


def _extracted(conn, row_id, record_id, text, etype, confidence=0.95, *, table=None,
               provider="huggingface", source_id="imessage", surface_detail=None):
    payload = {
        "record_id": record_id,
        "entity_text": text,
        "entity_type": etype,
        "confidence": confidence,
        "provider": provider,
        "source_id": source_id,
        "event_at": "2026-06-02T09:00:00Z",
    }
    if table:
        payload["canonical_table"] = table
    if surface_detail:
        payload["surface_detail"] = surface_detail
    _insert(
        conn,
        "message_entities",
        entity_id=row_id,
        record_id=record_id,
        source_id=source_id,
        entity_text=text,
        provider=provider,
        payload_json=json.dumps(payload),
    )


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import (
        ensure_conversation_messages_table,
        ensure_conversations_table,
    )

    c = sqlite3.connect(str(tmp_path / "repair.db"))
    apply_all_migrations(c)
    # conversation_messages is a canonical table the ingest lane creates, not
    # a migration; a node without it simply has no such rows to resolve to.
    ensure_conversations_table(c)
    ensure_conversation_messages_table(c)
    CanonicalTablesManager(c)  # ai_chat_messages, the same way
    # Canonical rows the mentions may cite.
    for mid in ("m1", "m2", "m3"):
        _insert(
            c, "conversation_messages",
            message_id=mid, conversation_id="conv-1", dataset_id="ds", sender_type="human",
            sender_id="+15550100", event_at="2026-06-01T12:00:00Z", content="Lunch with Ada Voss",
            source_id="imessage",
        )
    _insert(
        c, "ai_chat_messages",
        message_id="a1", conversation_id="chat-1", sender_type="human",
        event_at="2026-06-01T12:00:00Z", content="hello", source_id="chatgpt",
    )
    _insert(c, "journal_entries", entry_id="tl-1", content="run at Mill Pond", source_id="grow_journal")
    _insert(c, "location_events", event_id="tl-1-loc", place_name="Mill Pond", source_id="grow_journal")
    _insert(c, "activity_events", event_id="ev-1", source_id="browser_visits", occurred_at="2026-06-01T00:00:00Z")
    # The spine.
    _entity(c, "ent-ada", "Ada Voss", "person")
    _entity(c, "ent-plu", "Plurigrid", "org", aliases=["Plurigrid Inc"])
    _entity(c, "ent-austin", "Austin", "place")
    _entity(c, "ent-topos", "dialoguesai/topos", "project")
    c.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, known_usernames_json, is_self)"
        " VALUES ('c-maya', 'ds', 'src', 'Maya Chen', '[]', 0)"
    )
    _entity(c, "ent-maya", "Maya Chen", "person", contact_id="c-maya", identifiers=["maya@mudlark.studio"])
    c.commit()
    yield c
    c.close()


def _stamp(conn, mention_id):
    row = conn.execute(
        "SELECT canonical_table FROM entity_mentions WHERE mention_id=?", (mention_id,)
    ).fetchone()
    return row[0] if row else None


def _links(conn, record_id):
    return conn.execute(
        "SELECT entity_id, canonical_table, surface_text, confidence, event_at FROM entity_mentions"
        " WHERE record_id=? ORDER BY entity_id",
        (record_id,),
    ).fetchall()


def _writes(report):
    return (
        report["stamp"]["stamped"],
        report["restamp"]["restamped"],
        report["restamp"]["quarantined"],
        report["relink"]["linked"],
    )


# ------------------------------------------------------------- defect 1


def test_an_unstamped_mention_is_stamped_from_its_record(conn):
    _mention(conn, "u1", "ent-ada", "m1", "")
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["stamp"] == {"scanned": 1, "stamped": 1, "ambiguous": 0, "unresolved": 0}
    assert _stamp(conn, "u1") == "conversation_messages"


def test_an_unresolvable_unstamped_mention_is_left_unstamped(conn):
    _mention(conn, "u2", "ent-ada", "nowhere", None)
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["stamp"]["unresolved"] == 1
    assert _stamp(conn, "u2") is None
    assert report["restamp"]["quarantined"] == 0, "only a WRONG stamp is quarantined"


def test_an_ambiguous_unstamped_mention_is_left_alone(conn):
    _insert(conn, "calendar_events", event_id="ev-1", source_id="gcal", starts_at="2026-06-01T00:00:00Z")
    _mention(conn, "u3", "ent-ada", "ev-1", "")
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["stamp"]["ambiguous"] == 1
    assert _stamp(conn, "u3") == ""


# ------------------------------------------------------------- defect 3


def test_a_stamp_that_disagrees_with_the_row_is_repaired(conn):
    """The fan-out child stamped with its parent's group."""
    _mention(conn, "w1", "ent-austin", "tl-1-loc", "journal_entries", surface="Mill Pond")
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["restamp"]["restamped"] == 1
    assert report["restamp"]["by_table"]["journal_entries"]["restamped"] == 1
    assert _stamp(conn, "w1") == "location_events"


def test_a_stamp_citing_no_row_is_quarantined_not_deleted(conn):
    _entity(conn, "ent-ghost", "Ghost Writer", "person", mentions=5)
    _mention(conn, "w2", "ent-ghost", "gone", "ai_chat_messages", surface="Ghost Writer",
             confidence=0.77, event_at="2025-01-01T00:00:00Z", authored_by_owner=1)
    conn.commit()

    report = repair_mention_lineage(conn)

    assert report["restamp"]["quarantined"] == 1
    assert report["restamp"]["by_table"]["ai_chat_messages"]["quarantined"] == 1
    assert _stamp(conn, "w2") is None
    row = conn.execute(
        f"SELECT entity_id, record_id, canonical_table, surface_text, confidence, event_at,"
        f" authored_by_owner, reason FROM {QUARANTINE_TABLE} WHERE mention_id='w2'"
    ).fetchone()
    assert row == (
        "ent-ghost", "gone", "ai_chat_messages", "Ghost Writer", 0.77,
        "2025-01-01T00:00:00Z", 1, "record resolves to no canonical table",
    )
    # The entity no longer counts a mention it does not have.
    assert conn.execute(
        "SELECT mention_count FROM entities WHERE entity_id='ent-ghost'"
    ).fetchone()[0] == 0
    assert report["entities_recounted"] >= 1


def test_a_stamp_naming_an_unknown_table_is_recovered_from_the_row(conn):
    _mention(conn, "w3", "ent-ada", "m1", "timeline")
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["restamp"]["restamped"] == 1
    assert _stamp(conn, "w3") == "conversation_messages"


def test_a_correct_stamp_is_never_second_guessed(conn):
    _mention(conn, "ok1", "ent-ada", "m1", "conversation_messages")
    _mention(conn, "ok2", "ent-austin", "tl-1-loc", "location_events", surface="Mill Pond")
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["restamp"]["scanned"] == 0
    assert _stamp(conn, "ok1") == "conversation_messages"
    assert _stamp(conn, "ok2") == "location_events"


def test_a_row_claimed_by_two_tables_keeps_its_stamp(conn):
    """journal_entries says tl-1, and so would a calendar_events row with the
    same id: two answers is no answer."""
    _insert(conn, "calendar_events", event_id="tl-1", source_id="gcal", starts_at="2026-06-01T00:00:00Z")
    _mention(conn, "w4", "ent-ada", "tl-1", "ai_chat_messages")
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["restamp"]["ambiguous"] == 1
    assert _stamp(conn, "w4") == "ai_chat_messages"


# ------------------------------------------------------------- defect 2


def test_an_extracted_but_unlinked_row_is_linked_to_its_existing_entity(conn):
    _extracted(conn, "x1", "m2", "Ada Voss", "PER", table="conversation_messages")
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["relink"]["candidates"] == 1
    assert report["relink"]["linked"] == 1
    assert _links(conn, "m2") == [
        ("ent-ada", "conversation_messages", "Ada Voss", 0.95, "2026-06-02T09:00:00Z"),
    ]
    assert conn.execute("SELECT mention_count FROM entities WHERE entity_id='ent-ada'").fetchone()[0] == 1


def test_the_table_comes_from_the_record_when_the_payload_names_none(conn):
    """The unstamped lane's rows: the NER payload carries no table either."""
    _extracted(conn, "x2", "m3", "Ada Voss", "PER")
    _extracted(conn, "x3", "nowhere", "Ada Voss", "PER")
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["relink"]["linked"] == 1
    assert report["relink"]["unattributed"] == 1
    assert [r[1] for r in _links(conn, "m3")] == ["conversation_messages"]
    assert _links(conn, "nowhere") == []


def test_the_table_is_where_the_record_lives_not_what_the_payload_claims(conn):
    """A journal fan-out child's NER payload names its parent's table. Trusting
    it stamped 99 location_events rows journal_entries on the quarantined copy
    (2026-09-18) — the repair re-creating the defect its restamp pass fixes."""
    _extracted(conn, "x4", "tl-1-loc", "Austin", "LOC", table="journal_entries")
    _extracted(conn, "x5", "nowhere", "Austin", "LOC", table="journal_entries")
    conn.commit()
    first = repair_mention_lineage(conn)
    assert first["relink"]["linked"] == 1
    assert first["relink"]["unattributed"] == 1, "a claim with no row behind it links nothing"
    assert [r[1] for r in _links(conn, "tl-1-loc")] == ["location_events"]
    second = repair_mention_lineage(conn)
    assert second["restamp"]["scanned"] == 0, "the relink left nothing for the restamp pass"


def test_a_surface_that_matches_no_entity_is_not_minted(conn):
    _extracted(conn, "x4", "m2", "Zed Quill", "PER")
    _extracted(conn, "x5", "m2", "Ada Vos", "PER")  # a typo the fuzzy tier would take; not here
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    report = repair_mention_lineage(conn)
    assert report["relink"]["unresolved"] == 2
    assert report["relink"]["linked"] == 0
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == before
    assert _links(conn, "m2") == []


def test_the_writers_own_floor_and_value_labels_apply(conn):
    _extracted(conn, "x6", "m2", "Ada Voss", "PER", confidence=0.4)
    _extracted(conn, "x7", "m2", "Tuesday", "DATE", confidence=0.99)
    _extracted(conn, "x8", "m2", "##dy", "PER", confidence=0.99)
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["relink"]["skipped_low_confidence"] == 1
    assert report["relink"]["skipped_value_type"] == 1
    assert report["relink"]["skipped_invalid_surface"] == 1
    assert _links(conn, "m2") == []


def test_declared_rows_keep_their_spine_type_and_detail(conn):
    _extracted(conn, "x9", "ev-1", "dialoguesai/topos", "project", confidence=1.0,
               provider="declared", source_id="github_activity", table="activity_events",
               surface_detail="https://github.com/dialoguesai/topos/commit/abc")
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["relink"]["linked"] == 1
    assert _links(conn, "ev-1") == [
        ("ent-topos", "activity_events", "https://github.com/dialoguesai/topos/commit/abc", 1.0,
         "2026-06-02T09:00:00Z"),
    ]


def test_aliases_identifiers_and_contacts_resolve_without_fuzz(conn):
    _extracted(conn, "y1", "m1", "Plurigrid Inc", "ORG")          # alias
    _extracted(conn, "y2", "m2", "maya@mudlark.studio", "PER")    # identifier
    _extracted(conn, "y3", "m3", "Maya", "PER")                   # unique contact token
    # "Ada" alone is a <=3-char surface the writer rejects; "Voss" is the
    # unique-token person rule (one person carries that token, no contact).
    _extracted(conn, "y4", "a1", "Voss", "PER", table="ai_chat_messages")
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["relink"]["linked"] == 4
    assert [r[0] for r in _links(conn, "m1")] == ["ent-plu"]
    assert [r[0] for r in _links(conn, "m2")] == ["ent-maya"]
    assert [r[0] for r in _links(conn, "m3")] == ["ent-maya"]
    assert [r[0] for r in _links(conn, "a1")] == ["ent-ada"]


def test_an_owner_tombstone_and_an_unbind_are_honoured(conn):
    conn.execute(
        "INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key, note)"
        " VALUES ('ex-1', 'entity', 'zed quill', 'owner tombstone')"
    )
    conn.execute(
        "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id, score, kind, status)"
        " VALUES ('rev-1', 'voss', 'ent-ada', 1.0, 'no_bind', 'approved')"
    )
    _entity(conn, "ent-zed", "Zed Quill", "person")
    _extracted(conn, "z1", "m1", "Zed Quill", "PER")
    _extracted(conn, "z2", "m2", "Voss", "PER")  # the unique-token hit is unbound
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["relink"]["skipped_excluded"] == 1
    assert report["relink"]["unresolved"] == 1
    assert report["relink"]["linked"] == 0


def test_an_entity_already_linked_to_the_record_is_not_linked_twice(conn):
    _mention(conn, "have", "ent-ada", "m2", "conversation_messages", surface="Ada")
    _extracted(conn, "x10", "m2", "Ada Voss", "PER")
    conn.commit()
    report = repair_mention_lineage(conn)
    assert report["relink"]["already_linked"] == 1
    assert report["relink"]["linked"] == 0
    assert len(_links(conn, "m2")) == 1


# ------------------------------------------------------------ discipline


def _snapshot(conn):
    return (
        conn.execute("SELECT * FROM entity_mentions ORDER BY mention_id").fetchall(),
        conn.execute("SELECT entity_id, mention_count FROM entities ORDER BY entity_id").fetchall(),
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (QUARANTINE_TABLE,)
        ).fetchall(),
    )


def _seed_all_three(conn):
    _mention(conn, "u1", "ent-ada", "m1", "")
    _mention(conn, "w1", "ent-austin", "tl-1-loc", "journal_entries", surface="Mill Pond")
    _mention(conn, "w2", "ent-ada", "gone", "ai_chat_messages")
    _extracted(conn, "x1", "m2", "Ada Voss", "PER", table="conversation_messages")
    conn.commit()


def test_a_dry_run_counts_everything_and_writes_nothing(conn):
    _seed_all_three(conn)
    before = _snapshot(conn)
    report = repair_mention_lineage(conn, dry_run=True)
    assert report["dry_run"] is True
    assert _writes(report) == (1, 1, 1, 1)
    assert _snapshot(conn) == before


def test_the_repair_is_idempotent(conn):
    _seed_all_three(conn)
    first = repair_mention_lineage(conn)
    assert _writes(first) == (1, 1, 1, 1)
    second = repair_mention_lineage(conn)
    assert _writes(second) == (0, 0, 0, 0)
    assert second["relink"]["candidates"] == 0
    assert second["restamp"]["scanned"] == 0


def test_a_database_without_the_spine_is_skipped(tmp_path):
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    assert repair_mention_lineage(c)["skipped"] == "no entity_mentions table"


# ----------------------------------------------------------------- doors


def test_the_cli_prints_the_report(conn, monkeypatch, capsys):
    from topos import core
    from topos.features.entities.mention_lineage import main

    _seed_all_three(conn)
    monkeypatch.setattr(core.state, "get_db_connection", lambda: conn)
    assert main(["--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert _writes(report) == (1, 1, 1, 1)
    assert _stamp(conn, "u1") == "", "dry run wrote nothing"


def test_the_cli_refuses_without_a_database(monkeypatch, capsys):
    from topos import core
    from topos.features.entities.mention_lineage import main

    monkeypatch.setattr(core.state, "get_db_connection", lambda: None)
    assert main([]) == 1


def test_the_upgrade_target_runs_the_repair(conn):
    from topos.upgrades.runner import _exec_derived_rebuild

    _seed_all_three(conn)
    out = _exec_derived_rebuild({"params": {"targets": ["entity_mention_lineage"]}}, conn)
    assert _writes(out["targets"]["entity_mention_lineage"]) == (1, 1, 1, 1)
    assert _stamp(conn, "u1") == "conversation_messages"


def test_the_unreleased_manifest_declares_the_step():
    """The step is declared, with its ship notes, on whichever side of a release cut the tree is.

    It was staged under ``unreleased``, and the 1.4.0 cut stamped that block into
    ``1.4.0``. So the declaration is looked up in the staging block first, then in the
    newest shipped release that declares the id, the same declaration
    ``declaring_versions`` keys the upgrade ledger on. The newest release outright
    would go red again at the next cut, whose block does not carry this step.
    """
    from topos.upgrades import _validate_release, load_manifests

    step_id = "repair-entity-mention-lineage"
    # oldest -> newest, with the staging entry last
    declaring = [release for release in load_manifests(include_unreleased=True)
                 if any(s["id"] == step_id for s in release.get("steps") or [])]
    assert declaring, f"no release block, staged or shipped, declares {step_id}"
    release = declaring[-1]
    _validate_release(release)
    step = next(s for s in release["steps"] if s["id"] == step_id)
    assert step["kind"] == "derived_rebuild"
    assert step["params"]["targets"] == ["entity_mention_lineage"]
    assert step["consent"] == "auto"
    assert any("stopped-node" in note for note in release.get("notes") or []), (
        "the ship note must name the stopped-node upgrade lane"
    )
    assert any("corpus_mention_lineage.py" in note for note in release.get("notes") or []), (
        "the ship note must require the re-measurement before D8 is narrowed"
    )
