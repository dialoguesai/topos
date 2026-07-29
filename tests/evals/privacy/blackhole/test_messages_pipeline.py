"""The messages lane: canonical rows that mention a protected entity.

Messages carry no entity id of their own — the link lives in `entity_mentions` —
so this filter has two halves, and the tests check both independently. The join
is exact and survives any rephrasing of the name; the text scan catches a
spelling or nickname the resolver never bound. Neither alone is enough.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.blackhole_guard import (
    BlackholeGuard,
    CallerClass,
    owner_ui_guard,
)
from topos.storage.db.migrations import apply_all_migrations

pytestmark = [pytest.mark.bhlr, pytest.mark.private]

PROTECTED = "Dana Qx71reyes"
ALIAS = "Dqx72nickname"
VISIBLE = "Sam Ok91okoye"


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "messages.db"))
    apply_all_migrations(c)
    c.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json)
        VALUES ('ent-bh', 'person', ?, ?, ?)
        """,
        (PROTECTED, PROTECTED.lower(), f'["{ALIAS}"]'),
    )
    # rec-join is linked ONLY through the mention table: its text never names
    # the entity, so only the join can catch it.
    c.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table, surface_text)
        VALUES ('m1', 'ent-bh', 'rec-join', 'src', 'conversation_messages', ?)
        """,
        (PROTECTED,),
    )
    c.commit()
    BlackholeStore(c).blackhole_entity(entity_ref="ent-bh")
    yield c
    c.close()


def _grantee(conn) -> BlackholeGuard:
    return BlackholeGuard(conn, caller_class=CallerClass.GRANTEE)


def _rows():
    return [
        # caught by the mention join only — the text is innocuous
        {"record_id": "rec-join", "content": "see you at 7"},
        # caught by the text scan only — no mention row exists
        {"record_id": "rec-text", "content": f"lunch with {PROTECTED}"},
        # the alias path
        {"record_id": "rec-alias", "content": f"ping {ALIAS} about friday"},
        # must survive
        {"record_id": "rec-ok", "content": f"standup with {VISIBLE}"},
    ]


def test_mention_join_catches_a_record_whose_text_is_innocuous(conn):
    """The half a text scan cannot do: the row never names the entity."""
    kept = _grantee(conn).filter_canonical_rows(_rows())

    assert "rec-join" not in [r["record_id"] for r in kept]


def test_text_scan_catches_a_record_the_resolver_never_bound(conn):
    """The half the join cannot do: no mention row exists for this record."""
    kept = _grantee(conn).filter_canonical_rows(_rows())

    assert "rec-text" not in [r["record_id"] for r in kept]
    assert "rec-alias" not in [r["record_id"] for r in kept]


def test_unrelated_messages_survive(conn):
    kept = _grantee(conn).filter_canonical_rows(_rows())

    assert [r["record_id"] for r in kept] == ["rec-ok"]


def test_owner_keeps_every_message(conn):
    kept = owner_ui_guard(conn).filter_canonical_rows(_rows())

    assert len(kept) == 4


def test_blocked_record_ids_are_resolved_from_mentions(conn):
    assert _grantee(conn).blocked_record_ids() == {"rec-join"}
    assert owner_ui_guard(conn).blocked_record_ids() == set()


def test_alternate_id_keys_are_honoured(conn):
    """Different lanes name the column differently (message_id, id)."""
    guard = _grantee(conn)

    assert guard.filter_canonical_rows([{"message_id": "rec-join", "content": "x"}]) == []
    assert guard.filter_canonical_rows([{"id": "rec-join", "content": "x"}]) == []


def test_missing_mentions_table_does_not_crash_the_read(tmp_path):
    """A store predating the entity spine still has to serve messages."""
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    apply_all_migrations(c)
    c.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)
        VALUES ('e1', 'person', ?, ?)
        """,
        (PROTECTED, PROTECTED.lower()),
    )
    c.commit()
    BlackholeStore(c).blackhole_entity(entity_ref="e1")
    c.execute("DROP TABLE entity_mentions")
    c.commit()
    try:
        guard = BlackholeGuard(c, caller_class=CallerClass.GRANTEE)
        # The join half degrades to empty; the text half still protects.
        assert guard.blocked_record_ids() == set()
        kept = guard.filter_canonical_rows(
            [{"record_id": "r", "content": f"about {PROTECTED}"}]
        )
        assert kept == []
    finally:
        c.close()


# ------------------------------------------------- the pipeline entry point


def test_pipeline_requires_a_guard():
    """No default: a messages call site that has not decided who is asking
    fails loudly instead of serving protected rows."""
    from topos.uma_contact_enrichment import apply_message_contact_pipeline

    with pytest.raises(TypeError):
        apply_message_contact_pipeline(  # type: ignore[call-arg]
            [{"record_id": "r"}],
            conn=None,
            dataset_id=None,
            allowed_scopes=[],
            manifest=None,
            filters=None,
        )


def test_pipeline_early_exit_still_filters(conn):
    """The no-dataset_id path returns rows too, and those rows carry names."""
    from topos.uma_contact_enrichment import apply_message_contact_pipeline

    rows, _sidecar = apply_message_contact_pipeline(
        _rows(),
        conn=conn,
        dataset_id=None,  # early return path
        allowed_scopes=[],
        manifest=None,
        filters=None,
        blackhole_guard=_grantee(conn),
    )

    assert [r["record_id"] for r in rows] == ["rec-ok"]
