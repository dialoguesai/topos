"""
Gap: Provenance — missing provider/model → every write has provenance fields
Sprint: EN-P2-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.enrichment.job_writer import write_signal_records
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import ensure_migrations_applied

pytestmark = pytest.mark.gap


def test_provenance_on_signal_writes(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "prov.db"))
    ensure_migrations_applied(conn)
    bundle = AdapterFactory.create("local_database", conn=conn)
    write_signal_records(
        "emo_27",
        [
            {
                "message_id": "m1",
                "source_id": "chatgpt",
                "emotion_label": "joy",
                "confidence": 0.9,
                "provider": "huggingface",
                "model": "emo-model",
            }
        ],
        adapters=bundle,
        provenance={"provider": "huggingface", "model": "emo-model", "job_id": "emo_27"},
        conn=conn,
    )
    page = bundle.signal.get_by_dimension("memory")
    assert page.total >= 1
    assert page.items[0].get("provider") == "huggingface"
