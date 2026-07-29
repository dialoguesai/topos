"""`blackhole_status`: the least informative answer that supports the decision.

Home chat routing needs to know that protection exists. It must never learn
*who* is protected — this handler answers over the same channel a third-party
agent's request travels, so anything richer than a boolean would turn the
routing check into a disclosure channel of its own.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

import topos.core.handlers as hub
from topos.core.handlers.signal_features import handle_blackhole_status
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.storage.db.migrations import apply_all_migrations

pytestmark = [pytest.mark.bhlr, pytest.mark.private]

SECRET = "Dana Qx71reyes"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "status.db"))
    apply_all_migrations(c)
    c.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)
        VALUES ('ent-bh', 'person', ?, ?)
        """,
        (SECRET, SECRET.lower()),
    )
    c.commit()
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    yield c
    c.close()


def _status():
    return asyncio.run(handle_blackhole_status({"id": "r1", "type": "blackhole_status"}))


def test_reports_false_when_nothing_is_protected(conn):
    result = _status()

    assert result["status"] == "ok"
    assert result["payload"]["has_blackholes"] is False


def test_reports_true_once_something_is(conn):
    BlackholeStore(conn).blackhole_entity(entity_ref="ent-bh")

    assert _status()["payload"]["has_blackholes"] is True


def test_reports_pending_rebuild(conn):
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref="ent-bh")
    assert _status()["payload"]["pending_rebuild"] is True

    store.mark_rebuild_complete("ent-bh")
    assert _status()["payload"]["pending_rebuild"] is False


def test_answer_contains_no_names(conn):
    """The whole point: protection is disclosed, identity never is."""
    BlackholeStore(conn).blackhole_entity(entity_ref="ent-bh")

    blob = str(_status()).lower()

    assert SECRET.lower() not in blob
    assert "ent-bh" not in blob
    # Only the two booleans, nothing else that could carry a name.
    assert set(_status()["payload"]) <= {"has_blackholes", "pending_rebuild", "degraded"}


def test_unreadable_store_reports_protected(monkeypatch):
    """A node that cannot read its own flags must not answer "nothing is protected"."""

    class SickConn:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(hub, "get_db_connection", lambda: SickConn())

    payload = _status()["payload"]

    assert payload["has_blackholes"] is True
    assert payload["degraded"] is True


def test_missing_id_is_ignored(conn):
    """Consistent with every other handler: no id, no reply."""
    assert asyncio.run(handle_blackhole_status({"type": "blackhole_status"})) is None
