"""The node's own messenger sync writes past the pipeline's privacy stage; every row it writes still ends with a PII
disclosure. Each committed batch asks the disclosure sweep, and the sweep's walk fills the rows. Synthetic chat.db and
Signal stores in ``tmp_path`` only; the filter is the sweep tests' stand-in."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from tests.disclosure.test_disclosure_sweep import Filter, scrub
from tests.sources.test_imessage_spam_filter import _add_message, _make_chat_db, mac_ns
from tests.sources.test_messenger_sync_checkpoints import (  # noqa: F401 -- fixtures
    DAY,
    DS,
    NOW,
    _add_signal,
    _sync_imessage,
    _sync_signal,
    _synthetic_sources_only,
    signal_db,
)
from topos.disclosure import disclosure_sweep


@pytest.fixture
def asked(monkeypatch):
    import topos.config.settings as config

    monkeypatch.setattr(config.settings, "platform_privacy_via_engine", True)
    calls = []
    monkeypatch.setattr(disclosure_sweep, "request_run", lambda: calls.append(1))
    return calls


def _fill(path: Path) -> Filter:
    client = Filter()
    sweep = disclosure_sweep.Sweep(lambda: sqlite3.connect(path, check_same_thread=False), client=client, pause=0,
                                   poll=0, stage_active=lambda: False)
    assert asyncio.run(sweep.run(mode="pending"))["finished"]
    return client


def _undisclosed(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM conversation_messages WHERE content IS NOT NULL "
                            "AND trim(content) != '' AND content_disclosure_hash IS NULL").fetchone()[0]


def test_every_imessage_batch_asks_and_every_synced_row_ends_disclosed(tmp_path: Path, asked) -> None:
    chat_path = tmp_path / "chat.db"
    chat = _make_chat_db(chat_path)
    for rowid in range(1, 6):
        _add_message(chat, rowid=rowid, chat_id=1, handle_id=1, text=f"Mail alice@example.com about day {rowid}",
                     date=mac_ns(NOW - rowid * DAY))
    chat.close()
    path = tmp_path / "node.db"
    topos = sqlite3.connect(path, check_same_thread=False)
    _sync_imessage(topos, chat_path, mode="full_history", batch_size=2)
    assert len(asked) == 3                  # one request per committed batch (2 + 2 + 1 rows)
    assert _undisclosed(path) == 5
    client = _fill(path)
    assert len(client.ids) == 5 and _undisclosed(path) == 0
    stored = [row[0] for row in topos.execute("SELECT content_disclosure FROM conversation_messages")]
    assert stored and all("[EMAIL]" in text and "alice@example.com" not in text for text in stored)
    topos.close()


def test_every_signal_batch_asks_and_every_synced_row_ends_disclosed(tmp_path: Path, signal_db, asked) -> None:
    for n in range(3):
        _add_signal(signal_db, f"msg-{n}", int((NOW - (n + 1) * DAY) * 1000), f"Ask Alice about item {n}")
    path = tmp_path / "node.db"
    topos = sqlite3.connect(path, check_same_thread=False)
    _sync_signal(topos, mode="all", batch_size=2)
    assert len(asked) >= 2
    client = _fill(path)
    assert len(client.ids) == 3 and _undisclosed(path) == 0
    assert sorted(row[0] for row in topos.execute("SELECT content_disclosure FROM conversation_messages")) == sorted(
        scrub(f"Ask Alice about item {n}") for n in range(3))
    topos.close()
