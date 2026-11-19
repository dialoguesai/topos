"""Where a journal entry was written: kept exact, read by the owner only, coarsened on request (DR-77).

The owner's rulings of 18 Nov 2026: "Only me" (the owner's own questions and timeline read the position; an app
with a journal grant, a shared view and every other door does not) and "Make the exact option, with rounded options
too (like how the dates can be transformed. We want the option to limit precision to others.)".

Every position here is made up: a point in the open Atlantic, nowhere anyone lives.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_journal_time_log_ui_stream_ingest import TIME_LOG_SOURCE_DEF  # noqa: E402

import topos.core.handlers as hub  # noqa: E402
from topos.core import state as core_state  # noqa: E402
from topos.disclosure import coordinates as coords  # noqa: E402
from topos.principal import (  # noqa: E402
    OWNER_AGENT,
    OWNER_APP,
    RELAY_PRINCIPAL,
    THIRD_PARTY,
    Principal,
    reset_principal,
    set_principal,
)
from topos.storage.db.migrations import apply_all_migrations  # noqa: E402

LAT, LON, ACC, TAKEN = 12.3456789, -45.6789012, 8.5, "2026-10-01T09:00:05+00:00"
POSITION = {"latitude": LAT, "longitude": LON, "location_accuracy_m": ACC, "location_taken_at": TAKEN}
GROW = "grow_journal"
RAW = "raw_growjournal_ui_stream"
COLUMNS = ("latitude", "longitude", "location_accuracy_m", "location_taken_at")

OWNER = Principal(cls=OWNER_APP, channel="uds")
AUTOMATION = Principal(cls="owner_automation", channel="cp_relay", client_id="routine_executor")
AGENT = Principal(cls=OWNER_AGENT, channel="uds", client_id="coding-agent")
TPK = Principal(cls=THIRD_PARTY, channel="local_http", client_id="claude-desktop")
#: Every caller the legacy table tools serve that is not the owner's own app.
NON_OWNER_SERVED = {"cp_relay": RELAY_PRINCIPAL, "owner_automation": AUTOMATION, "legacy_none": None}


def _entry(n: int = 1, **extra: Any) -> Dict[str, Any]:
    """A record shaped like Grow's Cloud Function sends (grow_record_mapper), with synthetic text."""
    return {"id": f"grow-session-{n}", "startDate": "2026-10-01", "startTime": f"0{n}:00 AM",
            "endDate": "2026-10-01", "endTime": f"0{n}:30 AM", "duration": 30, "project": "Synthetic project",
            "goal": "A synthetic goal", "accomplished": f"A synthetic note {n}.", "completed": True,
            "location": "", "group": "Solo", **extra}


@pytest.fixture
def conn(tmp_path, pin_db_path, monkeypatch):
    db_file = tmp_path / "coords.db"
    pin_db_path(db_file)
    c = sqlite3.connect(str(db_file), check_same_thread=False)
    c.row_factory = sqlite3.Row
    apply_all_migrations(c)
    monkeypatch.setattr(core_state, "get_db_connection", lambda: c)
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    yield c
    c.close()


@pytest.fixture
def grow_source():
    from topos.sources.runtime_install import install_source_definition

    definition = {**TIME_LOG_SOURCE_DEF, "source_id": GROW, "display_name": "Grow Journal",
                  "schema_id": "grow.time_log.v1", "parser_id": "grow.time_log.v1",
                  "tables": [{**TIME_LOG_SOURCE_DEF["tables"][0], "table_id": "grow_journal_sessions",
                              "display_name": "Grow Journal Sessions"}]}
    handle = install_source_definition(definition)
    try:
        yield handle
    finally:
        handle.uninstall()


async def _ingest(record: Dict[str, Any], *, defer: bool = True) -> Dict[str, Any]:
    from topos.ingestion.ingest_helpers import _ingest_ui_payload_direct

    result = await _ingest_ui_payload_direct(dataset_id="user:default:device", schema_id="grow.time_log.v1",
                                             payload=record, job_id=f"job-{record['id']}", source_id=GROW,
                                             defer_enrichment=defer)
    assert result["status"] == "ok", result
    return result


def _position(conn, entry_id: str = "tl-grow-session-1") -> Optional[tuple]:
    row = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM journal_entries WHERE entry_id=?", (entry_id,)).fetchone()
    return None if row is None else tuple(row)


class _As:
    def __init__(self, principal):
        self.principal = principal

    def __enter__(self):
        self.token = set_principal(self.principal)

    def __exit__(self, *exc):
        reset_principal(self.token)


# ------------------------------------------------------------------ the migration


def test_migration_adds_four_nullable_columns_and_changes_no_row(tmp_path):
    from topos.storage.db.migrations.journal_entries_coordinates_v1 import apply_journal_entries_coordinates_v1_up

    c = sqlite3.connect(str(tmp_path / "old.db"))
    c.executescript("""
        CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT);
        CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, entry_at TEXT, content TEXT, place_name TEXT);
        INSERT INTO journal_entries VALUES ('tl-old', '2026-01-02T03:04:05', 'An older synthetic entry.', 'Somewhere');
    """)
    before = c.execute("SELECT * FROM journal_entries").fetchall()

    apply_journal_entries_coordinates_v1_up(c)
    apply_journal_entries_coordinates_v1_up(c)  # always_run: a second pass is a no-op

    info = {row[1]: row[2] for row in c.execute("PRAGMA table_info(journal_entries)")}
    assert {name: info[name] for name in COLUMNS} == {
        "latitude": "REAL", "longitude": "REAL", "location_accuracy_m": "REAL", "location_taken_at": "TEXT"}
    after = c.execute("SELECT entry_id, entry_at, content, place_name FROM journal_entries").fetchall()
    assert after == before
    assert c.execute(f"SELECT {', '.join(COLUMNS)} FROM journal_entries").fetchall() == [(None, None, None, None)]


def test_migration_is_registered_and_always_runs():
    from topos.storage.db.migrations.registry import MIGRATIONS

    spec = next(m for m in MIGRATIONS if m.id == "journal_entries_coordinates_v1")
    assert spec.always_run


# ------------------------------------------------------------------ what the normalizer keeps


@pytest.mark.parametrize("payload, expected", [
    (POSITION, (LAT, LON, ACC, TAKEN)),
    ({"latitude": "12.5", "longitude": "-45.25"}, (12.5, -45.25, None, None)),           # numeric text
    ({"latitude": 0, "longitude": 0, "location_accuracy_m": 0}, (0.0, 0.0, 0.0, None)),  # zeros are a position
    ({"latitude": 90, "longitude": -180}, (90.0, -180.0, None, None)),                   # the edges
    ({"latitude": 90.0001, "longitude": 0}, (None,) * 4),                                # out of range
    ({"latitude": 0, "longitude": 180.5}, (None,) * 4),
    ({"latitude": LAT}, (None,) * 4),                                                    # half a position
    ({"latitude": True, "longitude": False}, (None,) * 4),                               # booleans are not numbers
    ({"latitude": float("nan"), "longitude": 1.0}, (None,) * 4),
    ({**POSITION, "location_accuracy_m": -1}, (LAT, LON, None, TAKEN)),                 # bad accuracy alone drops
    ({**POSITION, "location_taken_at": "yesterday"}, (LAT, LON, ACC, None)),             # bad time alone drops
    ({}, (None,) * 4),
])
def test_coordinate_fields(payload, expected):
    fields = coords.coordinate_fields(payload)
    assert tuple(fields[name] for name in COLUMNS) == expected


def test_normalizer_carries_the_four_fields():
    from topos.ingestion.journal_time_log_normalize import normalize_time_log_payload

    out = normalize_time_log_payload(_entry(**POSITION), "grow-session-1", "ds")
    assert {name: out[name] for name in COLUMNS} == POSITION
    assert "latitude" not in json.dumps(out["_metadata"])
    bare = normalize_time_log_payload(_entry(), "grow-session-1", "ds")
    assert all(bare[name] is None for name in COLUMNS)


# ------------------------------------------------------------------ ingest keeps them


@pytest.mark.asyncio
async def test_ingest_keeps_the_exact_position_and_hands_enrichment_none(conn, grow_source):
    result = await _ingest(_entry(**POSITION))

    assert _position(conn) == (LAT, LON, ACC, TAKEN)
    # The raw retention table keeps the record as it arrived, as for every field.
    raw = json.loads(conn.execute(f"SELECT payload_json FROM {RAW}").fetchone()[0])
    assert {name: raw[name] for name in COLUMNS} == POSITION
    # No derived layer is handed the position.
    handed = result["_enrichment_ctx"]["canonical_records"]
    assert handed and all(not set(COLUMNS) & set(rec) for rec in handed)


@pytest.mark.asyncio
async def test_a_send_without_a_position_keeps_the_stored_one_and_a_new_position_replaces_it(conn, grow_source):
    await _ingest(_entry(**POSITION))
    await _ingest(_entry(accomplished="Edited synthetic note."))  # Grow leaves the fields out: kept
    assert _position(conn) == (LAT, LON, ACC, TAKEN)
    assert conn.execute("SELECT content FROM journal_entries").fetchone()[0].endswith("Edited synthetic note.")

    moved = {"latitude": -12.5, "longitude": 45.25, "location_accuracy_m": None, "location_taken_at": None}
    await _ingest(_entry(**moved))  # a new position replaces all four together
    assert _position(conn) == (-12.5, 45.25, None, None)


@pytest.mark.asyncio
async def test_an_unusable_position_is_not_stored(conn, grow_source):
    await _ingest(_entry(latitude=123.0, longitude=LON, location_accuracy_m=ACC, location_taken_at=TAKEN))
    assert _position(conn) == (None, None, None, None)


@pytest.mark.asyncio
async def test_app_ingest_door_passes_the_fields_through(conn, grow_source):
    from topos.core.handlers import handle_control_plane_request

    out = await handle_control_plane_request(
        {"id": "req-coords", "type": "app_ingest",
         "payload": {"user_id": "user-1", "dataset_id": "user-1:default:device1", "source_id": GROW,
                     "records": [_entry(**POSITION)]}},
        principal=OWNER,
    )
    assert out["status"] == "ok", out
    assert _position(conn) == (LAT, LON, ACC, TAKEN)


# ------------------------------------------------------------------ who reads them


async def _table_rows(table: str, principal, **payload: Any) -> list:
    from topos.core.handlers import handle_control_plane_request

    out = await handle_control_plane_request(
        {"id": f"req-{table}", "type": "get_table_rows", "payload": {"table_name": table, **payload}},
        principal=principal,
    )
    assert out["status"] == "ok", out
    return out["payload"]["rows"]


@pytest.mark.asyncio
async def test_the_owners_timeline_reads_the_exact_position(conn, grow_source):
    await _ingest(_entry(**POSITION))
    rows = await _table_rows("journal_entries", OWNER, filter_column="entry_id", filter_value="tl-grow-session-1")
    assert {name: rows[0][name] for name in COLUMNS} == POSITION
    raw = await _table_rows(RAW, OWNER)
    assert json.loads(raw[0]["payload_json"])["latitude"] == LAT


@pytest.mark.asyncio
@pytest.mark.parametrize("who", list(NON_OWNER_SERVED))
async def test_table_tools_withhold_the_position_from_everyone_else(conn, grow_source, who):
    await _ingest(_entry(**POSITION))
    principal = NON_OWNER_SERVED[who]

    rows = await _table_rows("journal_entries", principal)
    assert rows and all(not set(COLUMNS) & set(row) for row in rows)
    assert rows[0]["content"]  # the rest of the row is served as before
    raw = await _table_rows(RAW, principal)
    payload = json.loads(raw[0]["payload_json"])
    assert not set(COLUMNS) & set(payload) and payload["goal"] == "A synthetic goal"
    assert str(LAT) not in json.dumps(rows + raw)


@pytest.mark.asyncio
async def test_sorting_by_the_position_is_ignored_for_everyone_else(conn, grow_source):
    await _ingest(_entry(1, latitude=-60.0, longitude=LON))
    await _ingest(_entry(2, latitude=60.0, longitude=LON))
    default = [r["entry_id"] for r in await _table_rows("journal_entries", RELAY_PRINCIPAL)]
    for direction in ("asc", "desc"):
        sorted_rows = await _table_rows("journal_entries", RELAY_PRINCIPAL, order_by="latitude", order_dir=direction)
        assert [r["entry_id"] for r in sorted_rows] == default
    owner = await _table_rows("journal_entries", OWNER, order_by="latitude", order_dir="desc")
    assert [r["latitude"] for r in owner] == [60.0, -60.0]


@pytest.mark.asyncio
async def test_third_party_is_refused_the_table_tools_outright(conn, grow_source):
    from topos.core.handlers import handle_control_plane_request

    await _ingest(_entry(**POSITION))
    out = await handle_control_plane_request(
        {"id": "req-tp", "type": "get_table_rows", "payload": {"table_name": "journal_entries"}}, principal=TPK)
    assert out["status"] == "error" and "latitude" not in json.dumps(out)


@pytest.mark.asyncio
async def test_query_rows_carry_the_position_for_the_owners_app_only(conn, grow_source):
    from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore

    await _ingest(_entry(1, **POSITION))
    await _ingest(_entry(2))  # no position: no position keys at all
    store = SQLiteCanonicalStore(conn)

    with _As(OWNER):
        rows = {r["record_id"]: r for r in store.list("journal_entries", disclosure_tier="owner_raw").items}
    assert {name: rows["tl-grow-session-1"][name] for name in COLUMNS} == POSITION
    assert not set(COLUMNS) & set(rows["tl-grow-session-2"])

    for principal in (None, RELAY_PRINCIPAL, AUTOMATION, AGENT, TPK):
        for tier in ("owner_raw", "default_disclosure"):
            with _As(principal):
                items = store.list("journal_entries", disclosure_tier=tier).items
            assert items and all(not set(COLUMNS) & set(item) for item in items), (principal, tier)
    with _As(OWNER):  # even the owner's app, below the owner tier
        items = store.list("journal_entries", disclosure_tier="default_disclosure").items
    assert all(not set(COLUMNS) & set(item) for item in items)


def test_grantee_query_packet_drops_the_position_even_when_a_grant_names_a_transform():
    from topos.query.disclosure import DisclosureFilterPipeline
    from topos.query.types import RetrievalBundle

    row = {"_table": "journal_entries", "record_id": "tl-x", "content": "A synthetic note.", **POSITION}
    transforms = [{"table_id": "journal_entries", "field": "latitude", "transform_id": "coordinates_to_10km"}]
    out = DisclosureFilterPipeline().apply(RetrievalBundle(context_packet={"rows": [row]}),
                                           field_transforms=transforms, disclosure_tier="default_disclosure")
    served = out.context_packet["rows"]
    assert served and not set(COLUMNS) & set(served[0])
    assert coords.COORDINATES_SHAREABLE is False


def test_a_released_journal_item_cannot_carry_a_position():
    """permissions-v2 releases a journal entry as a fixed strict shape: an extra key is refused, not passed on."""
    from pydantic import ValidationError

    from topos.permissions_v2.knowledge_contract import JournalEntryResult

    opaque = "r." + "0" * 64
    base = {"kind": "journal_entry", "record_id": opaque, "content": "A synthetic note.",
            "source_ids": [GROW], "citations": [{"record_id": opaque, "source_id": GROW,
                                                "content": "A synthetic note."}]}
    JournalEntryResult.model_validate(base)  # the fixture is a valid release
    with pytest.raises(ValidationError):
        JournalEntryResult.model_validate({**base, "latitude": LAT, "longitude": LON})


# ------------------------------------------------------------------ the precision transforms


@pytest.mark.parametrize("transform, lat, lon, accuracy", [
    ("coordinates_to_100m", 12.346, -45.679, 100.0),
    ("coordinates_to_1km", 12.35, -45.68, 1_000.0),
    ("coordinates_to_10km", 12.3, -45.7, 10_000.0),
])
@pytest.mark.parametrize("named", ["latitude", "longitude"])
def test_each_transform_rounds_the_pair_as_specified(transform, lat, lon, accuracy, named):
    from topos.query.disclosure import _apply_field_transforms as query_transforms
    from topos.uma_filters import _apply_field_transforms, _apply_field_transforms_async
    from shared.filtering import FieldTransform

    row = {"_table": "journal_entries", "record_id": "tl-x", **POSITION}
    expected = {**row, "latitude": lat, "longitude": lon, "location_accuracy_m": accuracy}

    ft = FieldTransform(table_id="journal_entries", field=named, transform_id=transform)
    stats: Dict[str, Any] = {}
    assert _apply_field_transforms([row], [ft], table_id="journal_entries", diagnostics=stats) == [expected]
    assert stats["applied_count"] == 1
    assert asyncio.run(_apply_field_transforms_async([row], [ft], table_id="journal_entries")) == [expected]
    assert query_transforms([row], [{"field": named, "transform_id": transform}]) == [expected]
    # location_taken_at is a time: it coarsens with timestamp_to_date, not here.
    assert expected["location_taken_at"] == TAKEN


@pytest.mark.parametrize("value, places, expected", [
    (0.0005, 3, 0.001), (-0.0005, 3, -0.001), (0.0004999, 3, 0.0),
    (51.255, 2, 51.26), (-0.05, 1, -0.1), (89.99999, 1, 90.0), (-179.96, 1, -180.0),
])
def test_rounding_is_half_away_from_zero_on_the_decimal_value(value, places, expected):
    assert coords._round(value, places) == expected


def test_a_coarse_accuracy_is_kept_and_a_missing_one_becomes_the_cell():
    rounded = coords.round_position({**POSITION, "location_accuracy_m": 2_500.0}, "coordinates_to_1km")
    assert rounded["location_accuracy_m"] == 2_500.0
    rounded = coords.round_position({"latitude": LAT, "longitude": LON}, "coordinates_to_10km")
    assert rounded["location_accuracy_m"] == 10_000.0


def test_half_a_position_is_withheld_not_rounded():
    out = coords.round_position({"latitude": LAT, "location_accuracy_m": ACC}, "coordinates_to_1km")
    assert "latitude" not in out and "longitude" not in out and "location_accuracy_m" not in out


def test_the_transforms_are_in_the_catalog_and_misuse_is_refused():
    from shared.filtering import FILTER_CATALOG, FieldTransform
    from topos.query.disclosure import _apply_field_transforms as query_transforms

    for transform in coords.PRECISION_TRANSFORMS:
        assert transform in FILTER_CATALOG
        FieldTransform(table_id="journal_entries", field="latitude", transform_id=transform)
    from topos.uma_filters import UMAFilterError as Refused

    with pytest.raises(Refused):  # not a coordinate field
        query_transforms([{"content": "x"}], [{"field": "content", "transform_id": "coordinates_to_1km"}])
    with pytest.raises(Refused):  # one level, alone
        query_transforms([{**POSITION}], [{"field": "latitude",
                                           "transform_ids": ["coordinates_to_1km", "pii_redaction"]}])


# ------------------------------------------------------------------ delete scrubs them


def _tables_holding_the_position(conn) -> set:
    """Every table with a cell that holds the made-up position, as a number or inside text."""
    needles = (repr(LAT), repr(LON))
    found = set()
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        try:
            rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
        except sqlite3.Error:
            continue  # a virtual table this build cannot open
        for row in rows:
            for cell in tuple(row):
                if cell in (LAT, LON) or (isinstance(cell, str) and any(n in cell for n in needles)):
                    found.add(name)
    return found


@pytest.mark.asyncio
async def test_deleting_the_entry_leaves_no_copy_of_the_position(conn, grow_source):
    from topos.data_explorer_row_delete import delete_database_rows

    await _ingest(_entry(**POSITION), defer=False)  # the whole post-canonical pipeline runs
    assert _tables_holding_the_position(conn) == {"journal_entries", RAW}

    # Off the event loop, as the handler runs it: the delete takes the write gate.
    await asyncio.to_thread(delete_database_rows, conn, table_name="journal_entries",
                            row_ids=["tl-grow-session-1"], scope="full_lineage")

    assert _tables_holding_the_position(conn) == set()
