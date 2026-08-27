"""Rendering and indexing of derived signal objects.

The rendering IS the feature. A ``RelationshipEdge`` embedded as its payload
matches no sentence a person would type; the same edge embedded as "Mom — a
person in my personal circle" is reachable from "who's in my close circle"
without that phrasing being anticipated anywhere. So these tests assert on the
WORDS, not just on a row count.

The other half is what is deliberately NOT indexed: an edge keyed by a phone
number that resolves to nobody, and a diarization placeholder. Both would
publish an identifier or a non-person into a retrieval preview and retrieve
nothing useful in exchange.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List

import pytest

from topos.features.signal.derived_index import (
    DERIVED_RECORD_TYPES,
    DERIVED_SOURCE_ID,
    _NameResolver,
    index_derived_objects,
    is_derived_record_type,
    render_object,
)
from topos.storage.db.migrations import apply_all_migrations


class _FakeEncoder:
    """Deterministic 8-dim encoder. Records every text it was asked to embed."""

    seen: List[str] = []

    def run_inference(self, payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        texts = payload.get("texts") or []
        _FakeEncoder.seen.extend(str(t) for t in texts)
        return {
            "vectors": [[0.1] * 8 for _ in texts],
            "dims": 8,
            "model": config.get("model"),
            "provider": "fake",
        }


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "derived_index.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _add_object(
    conn: sqlite3.Connection,
    *,
    object_type: str,
    object_key: str,
    payload: Dict[str, Any],
    dimension: str = "relationships",
) -> str:
    object_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO signal_objects (
            object_id, signal_dimension, object_type, object_key, payload_json,
            confidence, source_refs_json, valid_from, valid_to, extractor_version,
            created_at, updated_at, created_by
        ) VALUES (?, ?, ?, ?, ?, 0.8, '[]', '2026-08-01', NULL, 'v1',
                  '2026-08-01', '2026-08-01', 'system')
        """,
        (object_id, dimension, object_type, object_key, json.dumps(payload)),
    )
    conn.commit()
    return object_id


def _add_contact(conn: sqlite3.Connection, identifier: str, display_name: str) -> None:
    contact_id = f"c-{identifier}"
    conn.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name) VALUES (?,?,?,?)",
        (contact_id, "ds", "imessage", display_name),
    )
    conn.execute(
        "INSERT INTO contact_identifiers (dataset_id, source_id, identifier, identifier_type, contact_id)"
        " VALUES (?,?,?,?,?)",
        ("ds", "imessage", identifier, "phone", contact_id),
    )
    conn.commit()


def _render(conn: sqlite3.Connection, object_id: str, **kwargs):
    row = conn.execute(
        "SELECT object_id, signal_dimension, object_type, object_key, payload_json"
        " FROM signal_objects WHERE object_id=?",
        (object_id,),
    ).fetchone()
    obj = {
        "object_id": row[0],
        "signal_dimension": row[1],
        "object_type": row[2],
        "object_key": row[3],
        "payload": json.loads(row[4]),
    }
    return render_object(obj, _NameResolver(conn), **kwargs)


class TestTheRenderingReadsLikeASentence:
    def test_a_personal_edge_says_personal_circle_in_words(self, conn) -> None:
        object_id = _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="grandma",
            payload={
                "target_entity_key": "grandma",
                "tier": "personal",
                "warmth_band": "medium",
                "cadence_band": "recent",
                "context_tags": ["holidays"],
            },
        )
        rendered = _render(conn, object_id)
        assert rendered is not None
        text = rendered.text.lower()
        assert "grandma" in text, rendered.text
        assert "personal circle" in text, (
            "the tier band never became words, so the edge cannot answer a "
            f"question phrased in ordinary language: {rendered.text!r}"
        )
        assert "holidays" in text
        # The kind hint is for the encoder only; a reader must never see it.
        assert "|" not in rendered.text, rendered.text
        assert rendered.header and rendered.header in rendered.embed_text

    def test_professional_and_personal_do_not_read_alike(self, conn) -> None:
        """The discriminating half. If both tiers render the same way, ranking
        between them is noise and "close circle" is answered by coin flip."""
        personal = _render(
            conn,
            _add_object(
                conn,
                object_type="RelationshipEdge",
                object_key="alex-doe",
                payload={"target_entity_key": "alex-doe", "tier": "personal"},
            ),
        )
        professional = _render(
            conn,
            _add_object(
                conn,
                object_type="RelationshipEdge",
                object_key="sam-roe",
                payload={"target_entity_key": "sam-roe", "tier": "professional"},
            ),
        )
        assert personal is not None and professional is not None
        assert "personal circle" in personal.text.lower()
        assert "personal circle" not in professional.text.lower()
        assert "colleague" in professional.text.lower()
        # "work"/"working" must not appear in ANY edge rendering: it made every
        # person a match for every work question (see _TIER_PHRASE).
        assert "work" not in personal.text.lower(), personal.text
        assert "work" not in professional.text.lower(), professional.text

    def test_a_phone_keyed_edge_renders_the_contact_name_not_the_number(self, conn) -> None:
        _add_contact(conn, "+15125550142", "Mom")
        rendered = _render(
            conn,
            _add_object(
                conn,
                object_type="RelationshipEdge",
                object_key="15125550142",
                payload={"target_entity_key": "15125550142", "tier": "personal"},
            ),
        )
        assert rendered is not None
        assert rendered.title == "Mom"
        assert "15125550142" not in rendered.text, (
            "the identifier reached the rendered text — a retrieval preview is a "
            f"disclosure surface: {rendered.text!r}"
        )

    def test_a_fact_renders_its_predicate_and_value_as_prose(self, conn) -> None:
        rendered = _render(
            conn,
            _add_object(
                conn,
                object_type="fact",
                object_key="fact:x:rel.relationship:mom",
                dimension="profile",
                payload={
                    "subject_entity_id": "ent-owner",
                    "predicate": "rel.relationship",
                    "object_value": json.dumps(
                        {"person": "mom", "role": "parent", "status": "active"}
                    ),
                    "disclosure": "owner_only",
                },
            ),
        )
        assert rendered is not None
        assert "mom is my parent" in rendered.text.lower(), rendered.text
        assert "{" not in rendered.text, f"raw JSON leaked into the rendering: {rendered.text!r}"
        assert rendered.disclosure == "owner_only"

    def test_a_truncated_object_value_still_renders_as_prose(self, conn) -> None:
        """Some stored values are clipped mid-string, so `json.loads` fails. The
        fallback must not be "embed the braces and escapes"."""
        rendered = _render(
            conn,
            _add_object(
                conn,
                object_type="fact",
                object_key="fact:x:mind.self_reported_state:burnt",
                dimension="profile",
                payload={
                    "subject_entity_id": "ent-owner",
                    "predicate": "mind.self_reported_state",
                    "object_value": '{"dimension": "stress", "report": "kinda burnt out this we',
                },
            ),
        )
        assert rendered is not None
        assert "kinda burnt out this we" in rendered.text
        assert '{"' not in rendered.text, rendered.text

    def test_a_dossier_keeps_its_summary_and_names_its_connections(self, conn) -> None:
        rendered = _render(
            conn,
            _add_object(
                conn,
                object_type="entity_dossier",
                object_key="dossier:ent-1",
                payload={
                    "entity_id": "ent-1",
                    "entity_type": "person",
                    "canonical_name": "Nicholas",
                    "summary_text": "Nicholas — person; 38 mentions across conversation_messages; last seen 2026-08-13.",
                    "top_connections": [{"canonical_name": "Kim", "edge_type": "co_occurrence"}],
                },
            ),
        )
        assert rendered is not None
        assert "Nicholas" in rendered.text
        assert "38 mentions" in rendered.text
        assert "Kim" in rendered.text


class TestWhatIsDeliberatelyNotIndexed:
    def test_an_unresolvable_identifier_key_is_skipped(self, conn) -> None:
        """No contact row for this number: rendering it would publish the
        number and retrieve nobody."""
        assert (
            _render(
                conn,
                _add_object(
                    conn,
                    object_type="RelationshipEdge",
                    object_key="15125550199",
                    payload={"target_entity_key": "15125550199", "tier": "personal"},
                ),
            )
            is None
        )

    def test_a_uuid_key_is_skipped(self, conn) -> None:
        # 60 of the 216 live edges are keyed by a bare uuid4 that resolves to no
        # contact and no entity. Shape is what the skip tests, so the value here
        # is deliberately low-entropy and obviously synthetic — a realistic uuid4
        # literal reads to a secret scanner as a credential (it is not one, and
        # naming the variable `key` is what made that ambiguous).
        uuid_shaped_key = "11111111-2222-4333-8444-555555555555"
        assert (
            _render(
                conn,
                _add_object(
                    conn,
                    object_type="RelationshipEdge",
                    object_key=uuid_shaped_key,
                    payload={"target_entity_key": uuid_shaped_key, "tier": "professional"},
                ),
            )
            is None
        )

    @pytest.mark.parametrize("placeholder", ["speaker-1", "unknown-0", "participant-2"])
    def test_diarization_placeholders_are_not_people(self, conn, placeholder) -> None:
        assert (
            _render(
                conn,
                _add_object(
                    conn,
                    object_type="RelationshipEdge",
                    object_key=placeholder,
                    payload={"target_entity_key": placeholder, "tier": "professional"},
                ),
            )
            is None
        )

    def test_a_superseded_object_is_not_indexed(self, conn, monkeypatch) -> None:
        monkeypatch.setattr(
            "topos.engine.backends.huggingface.HuggingFaceAdapter", _FakeEncoder, raising=True
        )
        object_id = _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="old-friend",
            payload={"target_entity_key": "old-friend", "tier": "personal"},
        )
        conn.execute(
            "UPDATE signal_objects SET valid_to='2026-08-02' WHERE object_id=?", (object_id,)
        )
        conn.commit()
        counts = index_derived_objects(conn)
        assert counts["written"] == 0
        assert _rows(conn) == []


def _rows(conn: sqlite3.Connection) -> List[Any]:
    return conn.execute(
        "SELECT record_id, record_type, text_preview FROM signal_embeddings WHERE source_id=?",
        (DERIVED_SOURCE_ID,),
    ).fetchall()


class TestTheIndexStaysCurrent:
    @pytest.fixture(autouse=True)
    def _fake_encoder(self, monkeypatch):
        _FakeEncoder.seen = []
        monkeypatch.setattr(
            "topos.engine.backends.huggingface.HuggingFaceAdapter", _FakeEncoder, raising=True
        )

    def test_a_pass_writes_one_row_per_object_and_stamps_the_record_type(self, conn) -> None:
        _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="grandma",
            payload={"target_entity_key": "grandma", "tier": "personal"},
        )
        counts = index_derived_objects(conn)
        assert counts["written"] == 1
        (record_id, record_type, preview), = _rows(conn)
        assert record_type == DERIVED_RECORD_TYPES["RelationshipEdge"]
        assert is_derived_record_type(record_type)
        assert "Grandma" in preview
        # The row joins back to the object it describes — the join that measured
        # zero before this existed.
        assert conn.execute(
            "SELECT COUNT(*) FROM signal_embeddings e JOIN signal_objects o"
            " ON e.record_id = o.object_id"
        ).fetchone()[0] == 1

    def test_a_second_pass_re_embeds_nothing(self, conn) -> None:
        _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="grandma",
            payload={"target_entity_key": "grandma", "tier": "personal"},
        )
        index_derived_objects(conn)
        _FakeEncoder.seen = []
        counts = index_derived_objects(conn)
        assert counts["written"] == 0 and counts["unchanged"] == 1
        assert _FakeEncoder.seen == [], (
            "an unchanged object was sent to the encoder again — the pass runs "
            "on every enrichment batch, so this is the difference between a "
            "no-op and a treadmill"
        )

    def test_a_changed_payload_re_embeds_that_object_only(self, conn) -> None:
        changing = _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="grandma",
            payload={"target_entity_key": "grandma", "tier": "personal"},
        )
        _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="uncle-ray",
            payload={"target_entity_key": "uncle-ray", "tier": "personal"},
        )
        index_derived_objects(conn)
        conn.execute(
            "UPDATE signal_objects SET payload_json=? WHERE object_id=?",
            (
                json.dumps(
                    {"target_entity_key": "grandma", "tier": "personal", "context_tags": ["holidays"]}
                ),
                changing,
            ),
        )
        conn.commit()
        _FakeEncoder.seen = []
        counts = index_derived_objects(conn)
        assert counts["written"] == 1 and counts["unchanged"] == 1
        assert len(_FakeEncoder.seen) == 1
        assert "holidays" in _FakeEncoder.seen[0]

    def test_an_object_that_stops_being_active_is_pruned(self, conn) -> None:
        object_id = _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="grandma",
            payload={"target_entity_key": "grandma", "tier": "personal"},
        )
        index_derived_objects(conn)
        assert len(_rows(conn)) == 1
        conn.execute(
            "UPDATE signal_objects SET valid_to='2026-08-05' WHERE object_id=?", (object_id,)
        )
        conn.commit()
        counts = index_derived_objects(conn)
        assert counts["pruned"] == 1
        assert _rows(conn) == [], (
            "a closed object kept its index row — the index would answer with "
            "people who are no longer in the data"
        )

    def test_one_person_reached_by_two_keys_yields_one_row(self, conn) -> None:
        _add_contact(conn, "+15125550142", "Alpine Xray")
        _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="15125550142",
            payload={"target_entity_key": "15125550142", "tier": "personal", "context_tags": ["austin"]},
        )
        _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="alpine-xray",
            payload={"target_entity_key": "alpine-xray", "tier": "personal"},
        )
        index_derived_objects(conn)
        rows = _rows(conn)
        assert len(rows) == 1, f"the same person was indexed twice: {rows!r}"
        assert "austin" in rows[0][2], "the richer of the two renderings should survive"

    def test_the_kill_switch_stops_the_writer(self, conn, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_DERIVED_OBJECT_INDEX", "off")
        _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="grandma",
            payload={"target_entity_key": "grandma", "tier": "personal"},
        )
        counts = index_derived_objects(conn)
        assert counts.get("disabled") == 1
        assert _rows(conn) == []


class TestAScrubTakesTheIndexWithIt:
    """A deletion request cannot be put on the enrichment schedule.

    Every sweep in `lifecycle/derived_scrub` matches rows by source_id, and the
    derived index has none — its rows are keyed by object_id and stamped with a
    synthetic source. So a scrub that closes the objects would leave sentences
    naming the scrubbed person in the index until the next enrichment batch
    happened to run.
    """

    @pytest.fixture(autouse=True)
    def _fake_encoder(self, monkeypatch):
        _FakeEncoder.seen = []
        monkeypatch.setattr(
            "topos.engine.backends.huggingface.HuggingFaceAdapter", _FakeEncoder, raising=True
        )

    def test_closing_the_object_orphans_the_row_until_pruned(self, conn) -> None:
        from topos.features.signal.derived_index import prune_orphaned_derived_embeddings

        object_id = _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="grandma",
            payload={"target_entity_key": "grandma", "tier": "personal"},
        )
        index_derived_objects(conn)
        conn.execute(
            "UPDATE signal_objects SET valid_to='2026-08-05' WHERE object_id=?", (object_id,)
        )
        conn.commit()
        assert len(_rows(conn)) == 1, "precondition: the row outlives the object"
        assert prune_orphaned_derived_embeddings(conn) == 1
        conn.commit()
        assert _rows(conn) == []

    def test_the_orphan_sweep_reaches_it(self, conn) -> None:
        """The wiring, not just the function: `sweep_orphans` is what a scrub
        actually calls."""
        from topos.features.lifecycle.derived_scrub import sweep_orphans

        object_id = _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="grandma",
            payload={"target_entity_key": "grandma", "tier": "personal"},
        )
        index_derived_objects(conn)
        conn.execute("DELETE FROM signal_objects WHERE object_id=?", (object_id,))
        conn.commit()
        assert sweep_orphans(conn)["derived_object_index"] == 1
        assert _rows(conn) == []

    def test_a_live_object_keeps_its_row(self, conn) -> None:
        """The control — a prune that deleted everything would pass both above."""
        from topos.features.lifecycle.derived_scrub import sweep_orphans

        _add_object(
            conn,
            object_type="RelationshipEdge",
            object_key="grandma",
            payload={"target_entity_key": "grandma", "tier": "personal"},
        )
        index_derived_objects(conn)
        assert sweep_orphans(conn)["derived_object_index"] == 0
        assert len(_rows(conn)) == 1
