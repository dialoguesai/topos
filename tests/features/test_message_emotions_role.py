"""Prov follow-up: message_emotions.role persists and filters wellbeing joins."""

from __future__ import annotations

import sqlite3

import pytest

from topos.enrichment.derived_tables import (
    DerivedTablesManager,
    reset_ensured_tables_cache,
)
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.migrations.message_emotions_role_v1 import (
    MIGRATION_ID,
    apply_message_emotions_role_v1_up,
)

pytestmark = [pytest.mark.check("C-quality-prov-followups")]


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "emo_role.db"))
    apply_all_migrations(c)
    # Wiki migrations may create a payload_json emotions shape; enrichment writes
    # the derived (message_id, model_name) shape — rebuild for this unit test.
    c.execute("DROP TABLE IF EXISTS message_emotions")
    c.commit()
    # Prior suite order can leave ``id(conn)`` in ``_ENSURED_CONNECTIONS`` after
    # GC reuses the pointer; clear so DROP + recreate is not skipped.
    reset_ensured_tables_cache()
    DerivedTablesManager(conn=c)._ensure_tables()
    apply_message_emotions_role_v1_up(c)
    yield c
    c.close()


def test_migration_adds_role_column(conn) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(message_emotions)").fetchall()}
    assert "role" in cols
    mid = conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert mid is not None


def test_write_emotions_batch_persists_role(conn) -> None:
    writer = DerivedTablesManager(conn=conn)
    n = writer.write_enrichment_batch(
        [
            {
                "message_id": "m_authored",
                "source_id": "imessage",
                "emotion_label": "joy",
                "confidence": 0.9,
                "model_name": "fake-emo",
                "all_emotions": [],
                "role": "authored",
            },
            {
                "message_id": "m_observed",
                "source_id": "imessage",
                "emotion_label": "anger",
                "confidence": 0.95,
                "model_name": "fake-emo",
                "all_emotions": [],
                "role": "observed",
            },
        ],
        "message_emotions",
    )
    assert n == 2
    rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT message_id, role FROM message_emotions ORDER BY message_id"
        ).fetchall()
    }
    assert rows == {"m_authored": "authored", "m_observed": "observed"}
    # Role filter fragment used by handlers: observed excluded, authored kept.
    kept = conn.execute(
        """
        SELECT message_id FROM message_emotions
        WHERE role IS NULL OR role IN ('authored', 'addressed')
        ORDER BY message_id
        """
    ).fetchall()
    assert kept == [("m_authored",)]
