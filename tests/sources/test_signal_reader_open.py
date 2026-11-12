"""Opening Signal Desktop's database: the binding, the key, and a count that reads no message.

protects: the reader named one binding (``pysqlcipher3``, source-only, needs a system SQLCipher) that
no install carried, so Signal could not be read from any install. It now takes whichever binding is
present, says in one sentence what to do when none is, and can count what a sync would read without
selecting a body — the number a person sees before they say yes. The last test opens a REAL encrypted
database when ``sqlcipher3`` is installed (it is, on macOS); elsewhere it is skipped, never passed.

Invented messages in a scratch file; nothing under ``~`` is opened and no Keychain is asked.
"""
from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest

from topos.ingestion.sources import signal_reader

pytestmark = pytest.mark.public

KEY = "ab" * 32
T0_MS = 1_760_000_000_000


def _schema(conn) -> None:
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, body TEXT, sent_at INTEGER, type TEXT, "
        "conversationId TEXT, sourceServiceId TEXT)"
    )
    rows = [
        ("m1", "the lantern is lit", T0_MS, "incoming", "conv-a", "peer-1"),
        ("m2", "wiring the beacon", T0_MS + 86_400_000, "outgoing", "conv-a", None),
        ("m3", "see you at the pier", T0_MS + 40 * 86_400_000, "incoming", "conv-b", "peer-2"),
    ]
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()


@pytest.fixture
def plain_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A plaintext Signal-shaped database behind stand-ins for every binding name."""
    for name in signal_reader.SQLCIPHER_BINDINGS:
        fake = types.ModuleType(name)
        fake.dbapi2 = sqlite3  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, fake)
        monkeypatch.setitem(sys.modules, f"{name}.dbapi2", sqlite3)
    path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(str(path))
    _schema(conn)
    conn.close()
    return path


def test_the_count_is_what_a_sync_would_read_and_selects_no_body(plain_signal: Path, monkeypatch) -> None:
    statements: list[str] = []
    real_open = signal_reader._open_signal_db

    def traced(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(signal_reader, "_open_signal_db", traced)
    everything = signal_reader.count_signal_messages(db_path=plain_signal, signal_key_hex=KEY)
    recent = signal_reader.count_signal_messages(start_unix=(T0_MS + 86_400_000) / 1000.0,
                                                 db_path=plain_signal, signal_key_hex=KEY)
    assert everything == {"messages": 3, "conversations": 2, "in_window": 3,
                          "oldest": T0_MS / 1000.0, "newest": (T0_MS + 40 * 86_400_000) / 1000.0}
    assert recent["messages"] == 3 and recent["in_window"] == 2
    rows = signal_reader.read_signal_rows(db_path=plain_signal, signal_key_hex=KEY)
    assert len(rows) == everything["messages"], "the count and the reader see the same rows"
    counting = [s for s in statements if "COUNT(" in s]
    assert counting and all("body" not in s and "sourceServiceId" not in s for s in counting)


def test_with_no_binding_the_reader_says_what_to_install(monkeypatch, tmp_path: Path) -> None:
    for name in signal_reader.SQLCIPHER_BINDINGS:
        monkeypatch.setitem(sys.modules, name, None)  # importing it now raises ImportError
        monkeypatch.setitem(sys.modules, f"{name}.dbapi2", None)
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a, **k: None)
    assert signal_reader.sqlcipher_available() is False
    with pytest.raises(ImportError) as excinfo:
        signal_reader.read_signal_rows(db_path=tmp_path / "db.sqlite", signal_key_hex=KEY)
    assert "topos-node[signal]" in str(excinfo.value)


def test_the_older_binding_still_opens_the_database_when_it_is_the_one_installed(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "sqlcipher3", None)
    monkeypatch.setitem(sys.modules, "sqlcipher3.dbapi2", None)
    fake = types.ModuleType("pysqlcipher3")
    fake.dbapi2 = sqlite3  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pysqlcipher3", fake)
    monkeypatch.setitem(sys.modules, "pysqlcipher3.dbapi2", sqlite3)
    path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(str(path))
    _schema(conn)
    conn.close()
    assert len(signal_reader.read_signal_rows(db_path=path, signal_key_hex=KEY)) == 3


def test_a_real_encrypted_database_opens_with_its_key_and_not_with_another(tmp_path: Path) -> None:
    """The one test here that uses SQLCipher itself: a database encrypted the way Signal Desktop's is
    (SQLCipher 4 defaults, a raw 32-byte key) is read with the right key and refused with a wrong one."""
    sqlcipher = pytest.importorskip("sqlcipher3.dbapi2")
    path = tmp_path / "db.sqlite"
    conn = sqlcipher.connect(str(path))
    conn.execute(f"PRAGMA key = \"x'{KEY}'\"")
    _schema(conn)
    conn.close()
    assert b"lantern" not in path.read_bytes() and not path.read_bytes().startswith(b"SQLite format 3")

    rows = signal_reader.read_signal_rows(db_path=path, signal_key_hex=KEY)
    assert [r["content"] for r in rows] == ["the lantern is lit", "wiring the beacon", "see you at the pier"]
    assert signal_reader.count_signal_messages(db_path=path, signal_key_hex=KEY)["messages"] == 3
    with pytest.raises(ValueError):
        signal_reader.read_signal_rows(db_path=path, signal_key_hex="cd" * 32)
