"""Local-first identity (topos/local_identity.py).

protects: a fresh database gets a `local:` owner and a UUID topos id exactly once; a
database with any identity is left alone; the move to an account rewrites every owner
column, every dataset id prefix and (when it changes) every topos id column in ONE
transaction, renames the raw folders, records the report, and refuses to run twice or on a
node that was never local.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.public

from topos import local_identity as li  # noqa: E402
from topos.core.state import get_engine_config_value, get_user_id  # noqa: E402

ACCOUNT = "user-kc-1234"
CP_TOPOS = "9a3f1c0e-0000-4000-8000-00000000cafe"


@pytest.fixture
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _fill(conn: sqlite3.Connection, user_id: str, topos_id: str) -> str:
    ds = f"{user_id}:topos:{topos_id}"
    conn.executescript(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY, owner_user_id TEXT, dataset_id TEXT, body TEXT);
        CREATE TABLE user_ingestion_sources (dataset_id TEXT, source_id TEXT, enabled INTEGER);
        CREATE TABLE routines (routine_id TEXT, user_id TEXT, title TEXT);
        CREATE TABLE bindings (resource_id TEXT, owner_id TEXT);
        CREATE TABLE other_people (user_id TEXT, name TEXT);
        CREATE TABLE raw_rows (id INTEGER PRIMARY KEY, payload_json TEXT, size INTEGER);
        CREATE TABLE installs (scope_key TEXT, source_id TEXT);
        CREATE VIEW recent AS SELECT * FROM messages;
        """
    )
    conn.execute("INSERT INTO raw_rows (payload_json, size) VALUES (?, 7)",
                 (json.dumps({"dataset_id": ds, "owner": user_id, "topos_id": topos_id, "text": "kept"}),))
    conn.execute("INSERT INTO raw_rows (payload_json, size) VALUES (?, 7)", (json.dumps({"dataset_id": "someone-else:topos:x"}),))
    conn.execute("INSERT INTO installs VALUES (?, 'claude_code_sessions')", (ds,))
    conn.executemany("INSERT INTO messages (owner_user_id, dataset_id, body) VALUES (?, ?, ?)",
                     [(user_id, ds, "hello"), (user_id, ds, "again"), ("someone-else", "someone-else:topos:x", "theirs")])
    conn.execute("INSERT INTO user_ingestion_sources VALUES (?, 'claude_code_sessions', 1)", (ds,))
    conn.execute("INSERT INTO user_ingestion_sources VALUES (?, 'imessage', 1)", (f"{user_id}:imessage",))
    conn.execute("INSERT INTO routines VALUES ('r1', ?, 'daily')", (user_id,))
    conn.execute("INSERT INTO bindings VALUES (?, ?)", (topos_id, user_id))
    conn.execute("INSERT INTO other_people VALUES ('friend-1', 'Pat')")
    conn.commit()
    return ds


# ----------------------------------------------------------------- minting
def test_a_fresh_database_gets_a_local_identity_once(conn):
    minted = li.ensure_local_identity(conn)
    assert minted is not None
    assert minted["user_id"].startswith("local:")
    uuid.UUID(minted["topos_id"])  # the control plane's shape, so an attach pairing can adopt it
    assert get_user_id(conn) == minted["user_id"]
    assert get_engine_config_value(conn, "topos_id") == minted["topos_id"]
    assert li.identity_mode(conn) == "local"

    assert li.ensure_local_identity(conn) is None  # a second start changes nothing
    assert get_user_id(conn) == minted["user_id"]


def test_a_database_with_an_account_identity_is_left_alone(conn):
    from topos.core.state import set_engine_config_value, store_user_id

    store_user_id(conn, ACCOUNT)
    set_engine_config_value(conn, "topos_id", CP_TOPOS)
    assert li.ensure_local_identity(conn) is None
    assert li.identity_mode(conn) == "account"
    assert get_user_id(conn) == ACCOUNT


def test_a_half_written_pairing_is_not_treated_as_fresh(conn):
    from topos.core.state import store_user_id

    store_user_id(conn, ACCOUNT)  # user id arrived, topos id not yet
    assert li.ensure_local_identity(conn) is None
    assert get_engine_config_value(conn, "topos_id") is None


def test_the_scope_names_the_mode(conn):
    from topos.ingestion.ai_tool_sync import resolve_owner_scope

    minted = li.ensure_local_identity(conn)
    scope = resolve_owner_scope(conn)
    assert scope["identity_mode"] == "local"
    assert scope["dataset_id"] == f"{minted['user_id']}:topos:{minted['topos_id']}"


# ----------------------------------------------------------------- the move
def test_connecting_an_account_moves_every_row_and_renames_the_raw_folders(conn, tmp_path):
    minted = li.ensure_local_identity(conn)
    old_user, old_topos = minted["user_id"], minted["topos_id"]
    _fill(conn, old_user, old_topos)
    raw = tmp_path / "raw"
    (raw / f"{old_user}_topos_{old_topos}".replace(":", "_")).mkdir(parents=True)
    (raw / f"{old_user}_imessage".replace(":", "_")).mkdir()
    (raw / "someone-else_topos_x").mkdir()

    report = li.rescope_to_account(conn, user_id=ACCOUNT, topos_id=CP_TOPOS, raw_base_path=raw)

    new_ds = f"{ACCOUNT}:topos:{CP_TOPOS}"
    assert get_user_id(conn) == ACCOUNT
    assert get_engine_config_value(conn, "topos_id") == CP_TOPOS
    assert li.identity_mode(conn) == "account"
    rows = conn.execute("SELECT owner_user_id, dataset_id FROM messages ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(ACCOUNT, new_ds), (ACCOUNT, new_ds), ("someone-else", "someone-else:topos:x")]
    assert sorted(r[0] for r in conn.execute("SELECT dataset_id FROM user_ingestion_sources")) == sorted(
        [new_ds, f"{ACCOUNT}:imessage"])
    assert conn.execute("SELECT user_id FROM routines").fetchone()[0] == ACCOUNT
    assert tuple(conn.execute("SELECT resource_id, owner_id FROM bindings").fetchone()) == (CP_TOPOS, ACCOUNT)
    assert conn.execute("SELECT user_id FROM other_people").fetchone()[0] == "friend-1"  # not the owner: untouched
    assert conn.execute("SELECT count(*) FROM messages WHERE dataset_id LIKE 'local:%'").fetchone()[0] == 0
    # Ids embedded in JSON payloads and in a differently named column move too.
    payload = json.loads(conn.execute("SELECT payload_json FROM raw_rows WHERE id=1").fetchone()[0])
    assert payload == {"dataset_id": new_ds, "owner": ACCOUNT, "topos_id": CP_TOPOS, "text": "kept"}
    assert json.loads(conn.execute("SELECT payload_json FROM raw_rows WHERE id=2").fetchone()[0]) == {
        "dataset_id": "someone-else:topos:x"}
    assert conn.execute("SELECT scope_key FROM installs").fetchone()[0] == new_ds
    assert conn.execute("SELECT size FROM raw_rows WHERE id=1").fetchone()[0] == 7  # non-text columns untouched

    assert report["topos_id_changed"] is True
    # Row updates per table and column: messages has two owner-bearing columns on two rows.
    assert report["tables"] == {"messages": 4, "user_ingestion_sources": 2, "routines": 1, "bindings": 2,
                                "raw_rows": 1, "installs": 1}
    assert sorted(report["raw_folders_renamed"]) == sorted([
        f"{old_user}_topos_{old_topos}".replace(":", "_"), f"{old_user}_imessage".replace(":", "_")])
    assert (raw / f"{ACCOUNT}_topos_{CP_TOPOS}").is_dir() and (raw / f"{ACCOUNT}_imessage").is_dir()
    assert (raw / "someone-else_topos_x").is_dir()
    stored = json.loads(get_engine_config_value(conn, li.RESCOPE_REPORT_KEY))
    assert stored["tables"] == report["tables"]
    previous = json.loads(get_engine_config_value(conn, li.PREVIOUS_LOCAL_IDENTITY_KEY))
    assert (previous["user_id"], previous["topos_id"]) == (old_user, old_topos)


def test_an_adopted_topos_id_keeps_the_local_one(conn):
    """Option B: the control plane registers the node's own topos id; only the owner moves."""
    minted = li.ensure_local_identity(conn)
    ds = _fill(conn, minted["user_id"], minted["topos_id"])

    report = li.rescope_to_account(conn, user_id=ACCOUNT, topos_id=minted["topos_id"], raw_base_path=None)

    assert report["topos_id_changed"] is False
    assert get_engine_config_value(conn, "topos_id") == minted["topos_id"]
    assert conn.execute("SELECT dataset_id FROM messages LIMIT 1").fetchone()[0] == \
        ds.replace(minted["user_id"], ACCOUNT)
    assert conn.execute("SELECT resource_id FROM bindings").fetchone()[0] == minted["topos_id"]
    assert report["tables"]["bindings"] == 1  # owner_id only; the topos id column never matched


def test_the_move_is_one_transaction(conn, monkeypatch):
    minted = li.ensure_local_identity(conn)
    _fill(conn, minted["user_id"], minted["topos_id"])
    # A table the sweep reaches late, with a trigger that refuses the owner write.
    conn.executescript(
        "CREATE TABLE zz_last (user_id TEXT); INSERT INTO zz_last VALUES ('%s');"
        "CREATE TRIGGER zz_refuse BEFORE UPDATE ON zz_last BEGIN SELECT RAISE(ABORT, 'no'); END;" % minted["user_id"]
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        li.rescope_to_account(conn, user_id=ACCOUNT, topos_id=CP_TOPOS, raw_base_path=None)

    assert get_user_id(conn) == minted["user_id"]  # nothing moved
    assert li.identity_mode(conn) == "local"
    assert conn.execute("SELECT count(*) FROM messages WHERE owner_user_id=?", (ACCOUNT,)).fetchone()[0] == 0


def test_the_move_refuses_an_account_node_and_a_second_run(conn):
    from topos.core.state import set_engine_config_value, store_user_id

    store_user_id(conn, ACCOUNT)
    set_engine_config_value(conn, "topos_id", CP_TOPOS)
    with pytest.raises(li.RescopeRefused):
        li.rescope_to_account(conn, user_id="user-other", topos_id=None, raw_base_path=None)
    assert get_user_id(conn) == ACCOUNT

    conn2 = sqlite3.connect(":memory:")
    conn2.row_factory = sqlite3.Row
    li.ensure_local_identity(conn2)
    li.rescope_to_account(conn2, user_id=ACCOUNT, topos_id=CP_TOPOS, raw_base_path=None)
    with pytest.raises(li.RescopeRefused):
        li.rescope_to_account(conn2, user_id="user-other", topos_id=None, raw_base_path=None)
    assert get_user_id(conn2) == ACCOUNT


def test_the_move_refuses_a_local_id_as_the_account(conn):
    li.ensure_local_identity(conn)
    with pytest.raises(li.RescopeRefused):
        li.rescope_to_account(conn, user_id="local:other", topos_id=None, raw_base_path=None)
