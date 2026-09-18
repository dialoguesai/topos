"""The entities job lands ``message_entities`` and the spine link together.

On a quarantined copy of a live node (2026-09-17) 11,637 ``conversation_messages``
rows, 2,751 ``ai_chat_messages`` rows and 894 ``activity_events`` rows had NER
output in ``message_entities`` and no ``entity_mentions`` row at all: the two
writes ran in different transactions on different connections, and the spine
half was wrapped in a ``try/except`` that logged a warning and moved on.
``message_entities`` cannot stand in for the missing link — zero of its
``entity_id`` values join the ``entities`` spine.

Now ``EntitiesJob.write_derived`` writes both under ONE ``batched_writes``
hold: a failure anywhere in the spine pass rolls the NER rows back with it and
propagates. Extracted mentions whose record names no canonical table are
refused from both tables and counted. Both orchestrator lanes route the job
through this hook.

**Mutation guard.** ``test_mutation_guard_no_unstamped_mention_survives_a_write``
fails if the stamp requirement is reverted in ``record_mention`` AND the
partition in ``write_derived`` is removed; ``test_a_failure_in_the_spine_pass
_rolls_the_extracted_rows_back`` fails if the two writes are split into
separate transactions again.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from topos.enrichment.derived_tables import DerivedTablesManager
from topos.enrichment.jobs.canonical import entities_job as ej
from topos.enrichment.jobs.canonical.entities_job import EntitiesJob, partition_by_lineage
from topos.features.entities.mention_lineage import MentionLineageError
from topos.storage.db.migrations import apply_all_migrations

ADA = {"entity_text": "Ada Voss", "entity_type": "PER", "confidence": 0.95}
AUSTIN = {"entity_text": "Austin", "entity_type": "LOC", "confidence": 0.90}
DATE = {"entity_text": "Tuesday", "entity_type": "DATE", "confidence": 0.99}


def _msg(message_id, content="Lunch with Ada Voss in Austin", **extra):
    row = {
        "message_id": message_id,
        "content": content,
        "source_id": "imessage",
        "event_at": "2026-06-01T12:00:00Z",
    }
    row.update(extra)
    return row


@pytest.fixture()
def conn(tmp_path):
    # The orchestrator lanes persist on worker threads; the injected
    # connection must allow cross-thread use, as core.state opens every real
    # connection. Direct write_derived calls run on the test thread.
    c = sqlite3.connect(str(tmp_path / "entities.db"), check_same_thread=False)
    apply_all_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def fake_ner(monkeypatch):
    """Route the NER batch through a canned per-record answer; no model."""

    def install(entities_by_record):
        async def _run(*_args, **kwargs):
            ids = list(kwargs.get("record_ids") or [])
            return SimpleNamespace(
                status="completed",
                output={
                    "items": [{"id": i, "entities": entities_by_record.get(i, [])} for i in ids],
                    "model": "fake-ner",
                    "provider": "huggingface",
                },
            )

        monkeypatch.setattr(ej, "run_engine_task", _run)

    monkeypatch.setenv("TOPOS_ENTITY_SPINE", "on")
    return install


def _run_job(conn, msgs):
    job = EntitiesJob(engine=object())
    records = asyncio.run(job.enrich(msgs))
    written = job.write_derived(records, msgs, tables_manager=DerivedTablesManager(conn))
    return job, records, written


def _rows(conn, sql, *params):
    return conn.execute(sql, params).fetchall()


def _mentions(conn):
    return _rows(conn, "SELECT record_id, canonical_table, surface_text FROM entity_mentions ORDER BY 1, 3")


def _extracted(conn):
    return _rows(conn, "SELECT record_id, entity_text, payload_json FROM message_entities ORDER BY 1, 2")


# ---------------------------------------------------------------- atomicity


def test_extracted_rows_and_spine_links_land_together(conn, fake_ner):
    fake_ner({"m1": [ADA, AUSTIN]})
    _job, _records, written = _run_job(conn, [_msg("m1", _table="conversation_messages")])

    assert written == 2
    extracted = _extracted(conn)
    assert [(r[0], r[1]) for r in extracted] == [("m1", "Ada Voss"), ("m1", "Austin")]
    # The message_entities payload carries the same stamp the spine row does.
    assert {json.loads(r[2])["canonical_table"] for r in extracted} == {"conversation_messages"}
    assert _mentions(conn) == [
        ("m1", "conversation_messages", "Ada Voss"),
        ("m1", "conversation_messages", "Austin"),
    ]


def test_a_failure_in_the_spine_pass_rolls_the_extracted_rows_back(conn, fake_ner, monkeypatch):
    """One transaction: the NER rows never outlive a spine pass that failed.

    Before, the rows were committed by one writer and the spine failure was
    logged by another — the exact shape of the 11,637 unlinked records.
    """
    from topos.features.entities.resolver import EntityResolver

    fake_ner({"m1": [ADA, AUSTIN]})
    calls = {"n": 0}
    real = EntityResolver.record_mention

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("spine write failed mid-batch")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(EntityResolver, "record_mention", flaky)
    job = EntitiesJob(engine=object())
    msgs = [_msg("m1", _table="conversation_messages")]
    records = asyncio.run(job.enrich(msgs))

    with pytest.raises(RuntimeError, match="mid-batch"):
        job.write_derived(records, msgs, tables_manager=DerivedTablesManager(conn))

    assert _extracted(conn) == [], "NER rows must roll back with the failed spine pass"
    assert _mentions(conn) == []
    assert _rows(conn, "SELECT COUNT(*) FROM entities")[0][0] == 0, (
        "entities minted before the failure roll back too"
    )


# ------------------------------------------------------------- the stamp


def test_a_record_that_names_no_table_is_refused_from_both_tables(conn, fake_ner):
    """Refused loudly, from BOTH tables — an extracted row without a spine
    link is the defect, not a lesser outcome. The stamped sibling in the same
    batch still lands."""
    fake_ner({"m1": [ADA], "m9": [ADA]})
    unstamped = _msg("m9")  # no _table, no canonical_table, no record_type
    job, _records, written = _run_job(conn, [_msg("m1", _table="conversation_messages"), unstamped])

    assert written == 1
    assert job.last_lineage_refusals == 1
    assert [(r[0], r[1]) for r in _extracted(conn)] == [("m1", "Ada Voss")]
    assert _mentions(conn) == [("m1", "conversation_messages", "Ada Voss")]


def test_the_table_is_derived_from_the_record_kind(conn, fake_ner):
    fake_ner({"a1": [ADA]})
    _run_job(conn, [_msg("a1", record_type="ai_chat_message", source_id="chatgpt")])
    assert _mentions(conn) == [("a1", "ai_chat_messages", "Ada Voss")]


def test_mutation_guard_no_unstamped_mention_survives_a_write(conn, fake_ner):
    """Reverting the stamp requirement writes rows this test refuses to see.

    A mixed batch — stamped, kind-only, and naming nothing — leaves exactly
    zero rows with an empty canonical_table, whichever way the lineage was
    resolved. ``require_canonical_table`` in ``record_mention`` is the last
    line; ``partition_by_lineage`` in ``write_derived`` is the first.
    """
    fake_ner({"m1": [ADA], "j1": [ADA], "x1": [ADA]})
    job, _records, _written = _run_job(
        conn,
        [
            _msg("m1", _table="conversation_messages"),
            _msg("j1", record_type="journal_entry", source_id="grow_journal"),
            _msg("x1"),
        ],
    )
    assert job.last_lineage_refusals == 1
    unstamped = _rows(
        conn, "SELECT COUNT(*) FROM entity_mentions WHERE COALESCE(canonical_table,'')=''"
    )[0][0]
    assert unstamped == 0
    assert {r[0] for r in _mentions(conn)} == {"m1", "j1"}
    assert {r[0] for r in _extracted(conn)} == {"m1", "j1"}


def test_record_mention_refuses_even_when_the_partition_is_bypassed(conn):
    """The resolver-level guard stands on its own (the mutation this pins)."""
    from topos.features.entities.resolver import EntityResolver

    resolver = EntityResolver(conn)
    entity_id, _ = resolver.resolve("Ada Voss", entity_type="person", record_id="m1")
    with pytest.raises(MentionLineageError):
        resolver.record_mention(entity_id, record_id="m1", surface_text="Ada Voss", canonical_table=None)
    assert _mentions(conn) == []


def test_partition_by_lineage_stamps_from_the_record_and_the_message():
    from_record = {"record_id": "r1", "entity_text": "Ada", "canonical_table": "journal_entry"}
    from_message = {"record_id": "m1", "entity_text": "Ada"}
    orphan = {"record_id": "x1", "entity_text": "Ada"}
    linkable, refused = partition_by_lineage(
        [from_record, from_message, orphan],
        [{"message_id": "m1", "_table": "conversation_messages"}, {"message_id": "x1"}],
    )
    assert [r["record_id"] for r in linkable] == ["r1", "m1"]
    assert from_record["canonical_table"] == "journal_entries"
    assert from_message["canonical_table"] == "conversation_messages"
    assert refused == [orphan]


def test_value_types_and_low_confidence_are_extracted_but_not_linked(conn, fake_ner):
    """Not every extracted row is a spine mention — the floor and the value
    labels are the writer's own rules, and they are unchanged."""
    fake_ner({"m1": [DATE, {"entity_text": "Ada Voss", "entity_type": "PER", "confidence": 0.3}]})
    _run_job(conn, [_msg("m1", _table="conversation_messages")])
    assert len(_extracted(conn)) == 2
    assert _mentions(conn) == []


# ------------------------------------------------------------- the lanes


def test_the_canonical_lane_persists_through_the_hook(conn, fake_ner, monkeypatch):
    from topos import core
    from topos.enrichment.orchestrator import EnrichmentOrchestrator

    fake_ner({"m1": [ADA, AUSTIN]})
    monkeypatch.setattr(core.state, "get_db_connection", lambda: conn)
    orchestrator = EnrichmentOrchestrator(tables_manager=DerivedTablesManager(conn))

    result = asyncio.run(
        orchestrator.run_canonical(
            [_msg("m1", _table="conversation_messages")], job_names=["entities"]
        )
    )

    assert result["errors"] == []
    assert result["records_created"]["message_entities"] == 2
    assert _mentions(conn) == [
        ("m1", "conversation_messages", "Ada Voss"),
        ("m1", "conversation_messages", "Austin"),
    ]


def test_the_canonical_lane_reports_a_failed_spine_pass_as_the_jobs_error(
    conn, fake_ner, monkeypatch
):
    """The old path swallowed the failure and left the NER rows behind."""
    from topos import core
    from topos.enrichment.orchestrator import EnrichmentOrchestrator
    from topos.features.entities.resolver import EntityResolver

    fake_ner({"m1": [ADA]})
    monkeypatch.setattr(core.state, "get_db_connection", lambda: conn)

    def boom(self, *args, **kwargs):
        raise RuntimeError("spine down")

    monkeypatch.setattr(EntityResolver, "record_mention", boom)
    orchestrator = EnrichmentOrchestrator(tables_manager=DerivedTablesManager(conn))
    result = asyncio.run(
        orchestrator.run_canonical(
            [_msg("m1", _table="conversation_messages")], job_names=["entities"]
        )
    )
    assert [e["job"] for e in result["errors"]] == ["entities"]
    assert _extracted(conn) == []
    assert _mentions(conn) == []


def test_the_signal_lane_routes_the_typed_write_through_the_hook(conn, fake_ner):
    """``write_signal_records`` takes the job's writer in place of the plain
    typed write; the facts that follow are unchanged."""
    from topos.enrichment.job_writer import write_signal_records
    from topos.storage.adapters.factory import AdapterFactory

    fake_ner({"m1": [ADA]})
    job = EntitiesJob(engine=object())
    msgs = [_msg("m1", _table="conversation_messages")]
    records = asyncio.run(job.enrich(msgs))
    bundle = AdapterFactory.create("local_database", conn=conn)

    count = write_signal_records(
        "entities",
        records,
        adapters=bundle,
        tables_manager=DerivedTablesManager(conn),
        conn=conn,
        derived_writer=lambda recs, tables: job.write_derived(recs, msgs, tables_manager=tables),
    )

    # write_signal_records counts the typed row AND the signal fact it files
    # for every entity; the hook changed where the row is written, not that.
    assert count == 2
    assert _mentions(conn) == [("m1", "conversation_messages", "Ada Voss")]
    assert [(r[0], r[1]) for r in _extracted(conn)] == [("m1", "Ada Voss")]


def test_the_signal_orchestrator_hands_the_hook_to_the_writer(monkeypatch):
    """The wiring itself: the lane passes a derived_writer built from the
    job's write_derived, so the signal path cannot fall back to the split
    write silently."""
    import inspect

    from topos.enrichment import orchestrator as orch

    src = inspect.getsource(orch.SignalDerivationOrchestrator._run_signal_derivation_inner)
    assert "derived_writer=derived_writer" in src
    assert 'getattr(_job_obj, "write_derived", None)' in src


def test_local_sync_records_are_stamped_before_enrichment():
    from topos.ingestion.local_sync import stamp_conversation_table

    rows = [{"message_id": "m1"}, {"message_id": "m2", "_table": "ai_chat_messages"}, "junk"]
    stamp_conversation_table(rows)
    assert rows[0]["_table"] == "conversation_messages"
    assert rows[1]["_table"] == "ai_chat_messages", "a declared table is kept"


def test_a_manager_without_a_connection_falls_through(monkeypatch):
    """Test fakes and connectionless managers: nothing to link against."""

    class _Manager:
        calls = []

        def write_enrichment_batch(self, records, table):
            self.calls.append((len(records), table))
            return len(records)

    job = EntitiesJob(engine=object())
    manager = _Manager()
    assert job.write_derived([{"record_id": "m1", "entity_text": "Ada"}], [], tables_manager=manager) == 1
    assert manager.calls == [(1, "message_entities")]
