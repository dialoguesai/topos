"""Private-window visits are withheld at the canonical write (OD-52 P1).

protects: the browser plugin sends Chrome's ``tab.incognito`` with every visit. The flag
reached the flat browser_visits row and nothing read it; the visit itself became an
activity row like any other, so it was embedded, clustered into interests, mined for
entity mentions and readable by any grant over activity. The canonical write now drops
the record before the mapper (which drops the flag): no activity row, nothing handed to
derivation, no timeline row. An unflagged visit is written exactly as before.

The raw layers (raw retention, the flat browser_visits table) keep what they kept: they
are the owner's own inspection surfaces, and every replay from them passes this same
withhold, so nothing downstream of the canonical write can see a private visit.

The withhold is the switch ``TOPOS_ACTIVITY_INCOGNITO_WITHHOLD``, on by default since
October 2026 (unset, as every test here leaves it unless it says otherwise): off (``0``,
``false``, ``no`` or ``off``), a flagged record is written like any other, as before.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict

import pytest

from topos.ingestion.canonical_pipeline import (
    INCOGNITO_WITHHOLD_FLAG,
    canonicalize_normalized_batch,
    incognito_withhold_enabled,
    is_incognito_record,
)
from topos.sources.registry import REGISTRY

# tests/ingestion is not a package: pytest (prepend import mode) puts this directory
# on sys.path, so sibling test modules import by their own name.
from test_ai_chat_writer_class import (  # noqa: F401  (fixtures)
    DATASET,
    OWNER,
    _relay,
    captured_jobs,
    conn,
)


@pytest.fixture(autouse=True)
def _the_default(monkeypatch):
    """The withhold under test is the default: the switch unset. The tests of the off switch set it off."""
    monkeypatch.delenv(INCOGNITO_WITHHOLD_FLAG, raising=False)


def _visit(url: str, **extra: Any) -> Dict[str, Any]:
    return {"url": url, "title": "A page", "visited_at": "2026-09-01T10:00:00Z", **extra}


def _write(msg_id: str, source_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": msg_id,
        "type": "app_ingest",
        "payload": {
            "user_id": OWNER,
            "dataset_id": DATASET,
            "source_id": source_id,
            "records": [record],
            "resource_id": f"dataset:{OWNER}:{DATASET}",
            "app_id": "browser-history-plugin",
            "requesting_user_id": OWNER,
        },
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _handed_to_derivation(jobs) -> list:
    return [r for j in jobs for r in (j.get("payload") or {}).get("canonical_records") or []]


@pytest.mark.parametrize("flag", [True, 1, "true", "TRUE", "1", " yes ", "on", 2])
def test_every_truthy_spelling_is_private(flag):
    assert is_incognito_record({"incognito": flag})
    assert is_incognito_record({"is_incognito": flag})
    assert is_incognito_record({"isIncognito": flag})


@pytest.mark.parametrize("flag", [False, 0, "false", "0", "", "no", None])
def test_a_false_or_absent_flag_is_not_private(flag):
    assert not is_incognito_record({"incognito": flag})
    assert not is_incognito_record({"url": "https://example.test/"})


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id,record", [
    ("browser_visits", _visit("https://example.test/private", incognito=True)),
    ("browser_events", {"event_type": "highlight", "url": "https://example.test/private", "title": "A page",
                        "content": "a selected span", "visited_at": "2026-09-01T10:00:00Z", "incognito": True}),
])
async def test_a_flagged_record_never_reaches_the_canonical_table(conn, captured_jobs, source_id, record):
    result = await _relay(_write("req-private", source_id, record))
    # Accepted, not an error: the plugin must not keep resending what is deliberately not kept.
    assert result["status"] == "ok", result
    assert _count(conn, "activity_events") == 0
    assert _count(conn, "timeline") == 0
    assert _handed_to_derivation(captured_jobs) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [{"incognito": False}, {}])
async def test_an_unflagged_visit_is_written_as_before(conn, captured_jobs, flag):
    url = "https://example.test/articles/one"
    assert (await _relay(_write("req-public", "browser_visits", _visit(url, **flag))))["status"] == "ok"
    row = dict(conn.execute(
        "SELECT event_id, activity_type, url, title, occurred_at, source_id, hostname, content "
        "FROM activity_events"
    ).fetchone())
    assert row == {
        "event_id": f"browser:{url}_2026-09-01T10:00:00Z",
        "activity_type": "visit",
        "url": url,
        "title": "A page",
        "occurred_at": "2026-09-01T10:00:00Z",
        "source_id": "browser_visits",
        "hostname": "example.test",
        "content": None,
    }
    assert [r["url"] for r in _handed_to_derivation(captured_jobs)] == [url]


@pytest.mark.parametrize("writer_class", ["cp_relay", None])
def test_a_mixed_batch_keeps_everything_but_the_private_record(conn, writer_class):
    """A file import or a replay from raw (no door) goes through the same withhold."""
    records = [
        {**_visit("https://example.test/a"), "record_id": "a"},
        {**_visit("https://example.test/private", incognito=True), "record_id": "p"},
        {**_visit("https://example.test/b", incognito="false"), "record_id": "b"},
    ]
    result = canonicalize_normalized_batch(conn, REGISTRY["browser_visits"], records, dataset_id=DATASET,
                                           sync_batch_id="batch-1", writer_class=writer_class)
    assert result.withheld_incognito == 1 and result.events_created == 2
    urls = sorted(r[0] for r in conn.execute("SELECT url FROM activity_events").fetchall())
    assert urls == ["https://example.test/a", "https://example.test/b"]
    assert sorted(r["url"] for r in result.canonical_records) == urls


@pytest.mark.asyncio
async def test_the_raw_layers_keep_what_they_kept(conn, captured_jobs):
    """The boundary, stated: the owner-only flat row still records the flag; nothing canonical exists."""
    assert (await _relay(_write("req-private", "browser_visits",
                                _visit("https://example.test/private", incognito=True))))["status"] == "ok"
    assert [tuple(r) for r in conn.execute("SELECT incognito FROM browser_visits").fetchall()] == [(1,)]
    assert _count(conn, "activity_events") == 0


def test_the_withhold_is_on_unless_the_owner_turns_it_off():
    assert incognito_withhold_enabled()  # the autouse fixture left it unset
    assert incognito_withhold_enabled({})
    for on in ("", "  ", "true", "1", "yes", "on", "TRUE", "enabled", "flase"):
        assert incognito_withhold_enabled({INCOGNITO_WITHHOLD_FLAG: on}), on
    for off in ("0", "false", "no", "off", " OFF ", "False", "NO"):
        assert not incognito_withhold_enabled({INCOGNITO_WITHHOLD_FLAG: off}), off


@pytest.mark.asyncio
async def test_off_a_flagged_visit_is_written_as_before(conn, captured_jobs, monkeypatch):
    monkeypatch.setenv(INCOGNITO_WITHHOLD_FLAG, "false")
    url = "https://example.test/private"
    assert (await _relay(_write("req-private", "browser_visits", _visit(url, incognito=True))))["status"] == "ok"
    assert [r[0] for r in conn.execute("SELECT url FROM activity_events").fetchall()] == [url]
    assert [r["url"] for r in _handed_to_derivation(captured_jobs)] == [url]


def test_off_a_mixed_batch_is_written_whole(conn, monkeypatch):
    monkeypatch.setenv(INCOGNITO_WITHHOLD_FLAG, "0")
    records = [{**_visit("https://example.test/a"), "record_id": "a"},
               {**_visit("https://example.test/private", incognito=True), "record_id": "p"}]
    result = canonicalize_normalized_batch(conn, REGISTRY["browser_visits"], records, dataset_id=DATASET,
                                           sync_batch_id="batch-off", writer_class="cp_relay")
    assert result.withheld_incognito == 0 and result.events_created == 2

