"""YouTube / session transcript ingest → transcripts group (ambient-by-default)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from topos.canonicalization.mappers import MAPPER_REGISTRY
from topos.canonicalization.mappers.transcript_mapper import TranscriptCanonicalMapper
from topos.core import state as core_state
from topos.enrichment.jobs.canonical.derivation_job import _iter_history
from topos.enrichment.jobs.canonical.relationship_edges_job import RelationshipEdgesJob
from topos.features.facts.extract import _is_owner_authored
from topos.features.provenance.roles import ROLE_AMBIENT, record_role
from topos.ingestion.canonical_pipeline import (
    canonicalize_normalized_batch,
    load_canonical_records_for_signal,
)
from topos.ingestion.parsers import PARSER_REGISTRY, TranscriptSessionParser
from topos.ingestion.parsers.base import NormalizedRecord
from topos.ingestion.parsers.transcript_parser import (
    shape_transcript_session,
    stitch_caption_items,
)
from topos.ingestion.sources.base import RawRecord
from topos.sources.bundled_canonical_triples import (
    VALID_CANONICAL_GROUP_IDS,
    infer_bundled_canonical_triple,
)
from topos.sources.registry import YOUTUBE_TRANSCRIPTS


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "data" / "transcripts"
FIXTURE_FILES = (
    "NVZwqkxEX6g.archive.json",
    "5B9EjKUFDFs.archive.json",
    "xdXLzFzxA9Q.archive.json",
)


def _load_archive(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


@pytest.fixture
def migrated_conn(tmp_path, pin_db_path):
    from topos.storage.db.migrations import apply_all_migrations

    db_file = tmp_path / "transcripts.db"
    pin_db_path(db_file)
    conn = sqlite3.connect(str(db_file), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    yield conn
    conn.close()


def test_registry_and_bundled_triple() -> None:
    assert YOUTUBE_TRANSCRIPTS.source_id == "youtube_transcripts"
    assert YOUTUBE_TRANSCRIPTS.source_type == "file"
    assert YOUTUBE_TRANSCRIPTS.delivery == "owner_upload"
    assert YOUTUBE_TRANSCRIPTS.posture == "ambient"
    assert YOUTUBE_TRANSCRIPTS.canonical_group_id == "transcripts"
    assert YOUTUBE_TRANSCRIPTS.discourse_lenses is True
    assert YOUTUBE_TRANSCRIPTS.canonical_mapper_id == "transcript"
    assert YOUTUBE_TRANSCRIPTS.enrichment_trigger == "automatic"
    assert infer_bundled_canonical_triple(schema_id="transcript.session.v1") == (
        "transcript",
        "transcripts",
    )
    assert "transcripts" in VALID_CANONICAL_GROUP_IDS
    assert PARSER_REGISTRY["transcript.session.v1"] is TranscriptSessionParser
    assert MAPPER_REGISTRY["transcript"] is TranscriptCanonicalMapper


def test_parser_drops_connector_role_fields() -> None:
    raw = {
        "transcript_id": "yt:demo",
        "origin_url": "https://youtu.be/demo",
        "participation_mode": "participated",
        "is_self": True,
        "is_from_self": 1,
        "is_owner": 1,
        "actor_role": "authored",
        "participants": [{"name": "Alex Rivera", "is_self": True}],
        "items": [{"text": "hello", "start": 0, "duration": 1, "is_self": True}],
    }
    parser = TranscriptSessionParser(dataset_id="user:default:device")
    assert parser.validate(RawRecord(record_id="x", payload=raw)).is_valid
    shaped = parser.parse(RawRecord(record_id="x", payload=raw)).payload
    assert "participation_mode" not in shaped
    assert "is_self" not in shaped
    assert "is_from_self" not in shaped
    assert "is_owner" not in shaped
    assert "actor_role" not in shaped
    assert shaped["participants"] == [{"name": "Alex Rivera"}]
    assert shaped["items"][0]["text"] == "hello"
    assert "is_self" not in shaped["items"][0]


def test_mapper_does_not_assign_unlabeled_segments_to_roster() -> None:
    payload = shape_transcript_session(
        {
            "transcript_id": "yt:demo",
            "started_at": "2026-06-01T10:00:00Z",
            "participants": [{"name": "Ada Lovelace"}],
            "items": [{"text": "Welcome", "start": 0.5, "duration": 2.0}],
        }
    )
    mapped = TranscriptCanonicalMapper().map_many(
        NormalizedRecord(record_id="yt:demo", payload=payload)
    )
    by_table: dict[str, list] = {}
    for rec in mapped:
        by_table.setdefault(rec.table, []).append(rec.payload)
    assert len(by_table["transcripts"]) == 1
    assert by_table["transcripts"][0]["participation_mode"] == "ambient"
    assert len(by_table["transcript_speakers"]) == 1
    speaker = by_table["transcript_speakers"][0]
    assert speaker["display_name"] == "Ada Lovelace"
    assert speaker["is_owner"] == 0
    assert speaker["contact_id"] is None
    assert speaker["attribution_source"] == "owner_roster"
    segment = by_table["transcript_segments"][0]
    assert segment["speaker_id"] is None
    assert segment["actor_role"] == "ambient"
    assert segment["is_from_self"] == 0


@pytest.mark.parametrize("filename", FIXTURE_FILES)
def test_fixture_archives_canonicalize_ambient(migrated_conn, filename) -> None:
    archive = _load_archive(filename)
    assert archive.get("schema") == "yt_transcript_archive"
    assert archive.get("items"), filename

    parser = TranscriptSessionParser(dataset_id="user:default:device")
    raw = RawRecord(record_id=str(archive.get("video_id") or filename), payload=archive)
    assert parser.validate(raw).is_valid
    normalized = parser.parse(raw)
    assert normalized.payload.get("asr_quality") == "generated"
    assert "participation_mode" not in normalized.payload

    result = canonicalize_normalized_batch(
        migrated_conn,
        YOUTUBE_TRANSCRIPTS,
        [normalized],
        dataset_id="user:default:device",
        sync_batch_id=f"batch-{filename}",
    )
    assert not result.errors
    raw_captions = sum(
        1 for item in archive["items"] if str(item.get("text") or "").strip()
    )
    stitched_items = normalized.payload["items"]
    assert len(stitched_items) < raw_captions
    segment_count = migrated_conn.execute(
        "SELECT COUNT(*) FROM transcript_segments WHERE source_id=?",
        ("youtube_transcripts",),
    ).fetchone()[0]
    assert segment_count == len(stitched_items)
    assert migrated_conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1

    roles = {
        row[0]
        for row in migrated_conn.execute("SELECT DISTINCT actor_role FROM transcript_segments")
    }
    assert roles == {"ambient"}
    assert migrated_conn.execute(
        "SELECT COUNT(*) FROM transcript_segments WHERE is_from_self!=0"
    ).fetchone()[0] == 0
    assert migrated_conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
    assert migrated_conn.execute(
        "SELECT COUNT(*) FROM transcript_speakers WHERE is_owner!=0 OR contact_id IS NOT NULL"
    ).fetchone()[0] == 0
    participation = migrated_conn.execute(
        "SELECT participation_mode FROM transcripts"
    ).fetchone()[0]
    assert participation == "ambient"

    for rec in result.canonical_records:
        assert rec.get("_table") == "transcript_segments"
        assert rec.get("actor_role") == "ambient"
        assert rec.get("is_from_self") == 0
        assert record_role(rec, table="transcript_segments") == ROLE_AMBIENT
        assert _is_owner_authored(rec, "transcript_segments") is False

    import asyncio

    produced = asyncio.run(RelationshipEdgesJob().enrich(result.canonical_records))
    assert produced == []

    history = _iter_history(migrated_conn, limit=50)
    transcript_history = [row for row in history if row["table"] == "transcript_segments"]
    assert transcript_history == []


def test_stitch_joins_adjacent_open_captions() -> None:
    items = stitch_caption_items(
        [
            {"text": "The new generation of OpenAI", "start": 1.68, "duration": 3.12},
            {"text": "called Astra.", "start": 4.50, "duration": 2.05},
            {"text": "I have early access.", "start": 6.80, "duration": 1.90},
        ]
    )
    assert [item["text"] for item in items] == [
        "The new generation of OpenAI called Astra.",
        "I have early access.",
    ]
    assert items[0]["start"] == 1.68
    assert items[0]["stitched_lines"] == 2
    assert items[0]["duration"] == pytest.approx((4.50 + 2.05) - 1.68)


def test_stitch_breaks_on_gap_speaker_and_marker() -> None:
    items = stitch_caption_items(
        [
            {"text": "Hello from", "start": 0.0, "duration": 1.0, "speaker": "A"},
            {"text": "the other room", "start": 1.05, "duration": 1.0, "speaker": "B"},
            {"text": "still talking", "start": 4.5, "duration": 1.0, "speaker": "B"},
            {"text": "[music]", "start": 5.4, "duration": 2.0},
            {"text": "After the break", "start": 7.5, "duration": 1.0},
        ]
    )
    texts = [item["text"] for item in items]
    assert "Hello from" in texts
    assert "the other room" in texts
    assert "[music]" in texts
    assert "After the break" in texts
    assert all("Hello from the other" not in t for t in texts)


def test_parser_stitches_archive_before_canonical() -> None:
    raw = {
        "transcript_id": "yt:astra",
        "origin_url": "https://youtu.be/astra",
        "items": [
            {"text": "generation of OpenAI", "start": 0.48, "duration": 3.12},
            {"text": "called Astra", "start": 3.40, "duration": 2.00},
        ],
    }
    shaped = TranscriptSessionParser(dataset_id="user:default:device").parse(
        RawRecord(record_id="yt:astra", payload=raw)
    ).payload
    assert len(shaped["items"]) == 1
    assert "OpenAI" in shaped["items"][0]["text"]
    assert "Astra" in shaped["items"][0]["text"]
    mapped = TranscriptCanonicalMapper().map_many(
        NormalizedRecord(record_id="yt:astra", payload=shaped)
    )
    segments = [rec.payload for rec in mapped if rec.table == "transcript_segments"]
    assert len(segments) == 1
    assert "OpenAI" in segments[0]["content"] and "Astra" in segments[0]["content"]
    assert segments[0]["actor_role"] == "ambient"
    assert segments[0]["is_from_self"] == 0


def test_reingest_replaces_pre_stitch_segment_ids(migrated_conn) -> None:
    parser = TranscriptSessionParser(dataset_id="user:default:device")
    archive = {
        "transcript_id": "yt:astra",
        "origin_url": "https://youtu.be/astra",
        "started_at": "2026-06-01T10:00:00Z",
        "items": [
            {"text": "generation of OpenAI", "start": 0.48, "duration": 3.12},
            {"text": "called Astra", "start": 3.40, "duration": 2.00},
        ],
    }
    migrated_conn.execute(
        """
        INSERT INTO transcripts (
            transcript_id, dataset_id, title, origin_kind, participation_mode,
            asr_quality, source_id, source_record_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "yt:astra",
            "user:default:device",
            "astra",
            "youtube",
            "ambient",
            "generated",
            "youtube_transcripts",
            "yt:astra",
        ),
    )
    for index, text in enumerate(("generation of OpenAI", "called Astra")):
        migrated_conn.execute(
            """
            INSERT INTO transcript_segments (
                segment_id, transcript_id, dataset_id, content, start_sec,
                actor_role, is_from_self, source_id, source_record_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"yt:astra:{index}",
                "yt:astra",
                "user:default:device",
                text,
                float(index),
                "ambient",
                0,
                "youtube_transcripts",
                f"yt:astra:{index}",
            ),
        )
    migrated_conn.commit()
    normalized = parser.parse(RawRecord(record_id="yt:astra", payload=archive))
    result = canonicalize_normalized_batch(
        migrated_conn,
        YOUTUBE_TRANSCRIPTS,
        [normalized],
        dataset_id="user:default:device",
        sync_batch_id="replace-stale",
    )
    assert not result.errors
    ids = {
        row[0]
        for row in migrated_conn.execute(
            "SELECT segment_id FROM transcript_segments WHERE transcript_id=?",
            ("yt:astra",),
        )
    }
    assert ids == {"yt:astra:480"}
    assert migrated_conn.execute(
        "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id=?",
        ("yt:astra",),
    ).fetchone()[0] == 1
    content = migrated_conn.execute(
        "SELECT content FROM transcript_segments WHERE segment_id=?",
        ("yt:astra:480",),
    ).fetchone()[0]
    assert "OpenAI" in content and "Astra" in content


def test_three_fixtures_and_idempotent_reingest(migrated_conn) -> None:
    parser = TranscriptSessionParser(dataset_id="user:default:device")
    first_counts = {}
    for filename in FIXTURE_FILES:
        archive = _load_archive(filename)
        normalized = parser.parse(
            RawRecord(record_id=str(archive.get("video_id")), payload=archive)
        )
        result = canonicalize_normalized_batch(
            migrated_conn,
            YOUTUBE_TRANSCRIPTS,
            [normalized],
            dataset_id="user:default:device",
            sync_batch_id=f"first-{filename}",
        )
        assert not result.errors
        first_counts[filename] = migrated_conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id=?",
            (normalized.record_id,),
        ).fetchone()[0]
        assert first_counts[filename] > 0

    assert migrated_conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 3
    assert migrated_conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0

    # Re-ingest the first archive: counts stay put.
    archive = _load_archive(FIXTURE_FILES[0])
    normalized = parser.parse(
        RawRecord(record_id=str(archive.get("video_id")), payload=archive)
    )
    canonicalize_normalized_batch(
        migrated_conn,
        YOUTUBE_TRANSCRIPTS,
        [normalized],
        dataset_id="user:default:device",
        sync_batch_id="reingest",
    )
    assert migrated_conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 3
    assert migrated_conn.execute(
        "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id=?",
        (normalized.record_id,),
    ).fetchone()[0] == first_counts[FIXTURE_FILES[0]]

    reloaded = load_canonical_records_for_signal(migrated_conn, YOUTUBE_TRANSCRIPTS, limit=20)
    assert reloaded
    assert all(rec.get("_table") == "transcript_segments" for rec in reloaded)
    assert all(rec.get("actor_role") == "ambient" for rec in reloaded)


@pytest.mark.asyncio
async def test_file_ingest_writes_ambient_rows(migrated_conn, monkeypatch, tmp_path) -> None:
    from topos.ingestion import canonical_pipeline as pipeline
    from topos.ingestion.ingest_helpers import ingest_file_payload

    async def _skip_enrichment(**kwargs):
        return {"signal_derivation": {}, "canonical_enrichment": {}}

    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)
    monkeypatch.setattr(pipeline, "run_post_canonical_pipeline", _skip_enrichment)
    archive = _load_archive("5B9EjKUFDFs.archive.json")
    path = tmp_path / "clip.archive.json"
    path.write_text(json.dumps(archive))

    result = await ingest_file_payload(
        dataset_id="user:default:device",
        schema_id="transcript.session.v1",
        file_path=str(path),
        file_format="json",
        job_id="job-yt-file-1",
        source_id=YOUTUBE_TRANSCRIPTS.source_id,
    )
    assert result.get("status") == "ok"
    assert migrated_conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1
    assert migrated_conn.execute(
        "SELECT COUNT(*) FROM transcript_segments WHERE actor_role!='ambient'"
    ).fetchone()[0] == 0
    assert migrated_conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
