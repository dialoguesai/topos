"""The owner's first-party capture apps keep their sources provable on every node (source continuity).

protects: two of the owner's sources stopped gaining rows a grant can count, and a node installed today
would have stopped the same way, with nothing for its owner to do about it.

  - Browser visits. ``activity_events`` recorded no writer unless the owner set
    ``TOPOS_ACTIVITY_WRITER_CLASS``, so every visit the plugin pushed arrived with no writer. Such a row
    is the owner's only while a receipt lists it, so each day's visits waited for the owner's next
    receipt, on every node, and a receipt over unrecorded rows cannot tell the plugin's visits from
    another app's. The switch is now on by default. The plugin's stamped visit records ``owner_app``,
    the plugin's app id and the install's dataset, and counts the moment the owner has attested the
    plugin once, whenever that was. The door also binds an activity row to the source's install, as it
    already did a journal row, so a plugin attached under another name for the same store still proves.
  - Journal entries pushed by the Grow app. The control plane did not stamp the app, so the node recorded
    each push as ``cp_relay``: a write nobody vouched for, which no receipt can ever cover. The control
    plane now stamps ``grow-app`` on ``grow_journal`` (CP ``OWNER_CAPTURE_APP_DEFAULT``); the node records
    the stamp and counts it once the owner has attested ``grow-app``. A receipt the owner made under a
    provisional app id lists the older rows but never proves a push under the real one.

  - File imports (the Grow data file, a ChatGPT export). The import door recorded the dataset the web
    app uploaded into (``<owner>:default:<device>``), while the install that binds the source to the
    owner names ``<owner>:topos:<id>``, so a new import by the owner's own door never proved, and no
    receipt can list it (it has a writer). The import door now binds those rows to the install as the
    ``app_ingest`` door does (one list: ``capture_receipts.bound_to_install``).

What must not change, and is pinned here: a row counts only when the node can show the owner's own
attested capture app (or the owner's import door) wrote it, into the install that binds the source to
the owner. An unstamped push, another app's push, an app attested for another source and a push
authorised for someone else's dataset never count, a ``cp_relay`` row is never offered to a receipt, and
switching recording off brings back exactly the old behaviour. A private-window visit gets no canonical
row by default.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

import pytest

from topos.ingestion.canonical_pipeline import INCOGNITO_WITHHOLD_FLAG
from topos.permissions_v2 import capture_receipts as cr
from topos.permissions_v2.canonical import PolicyError
from topos.principal import OWNER_APP
from topos.storage.canonical.canonical_store import ACTIVITY_WRITER_FLAG

# tests/ingestion is not a package: sibling test modules import by their own name.
from test_ai_chat_writer_class import (  # noqa: F401  (fixtures)
    GRANTEE, OWNER, _export_line, _relay, _stamp, _start_ingestion, captured_jobs, conn)
from test_canonical_writer_class import _rows
from test_journal_push_provenance import AUTHORISED, DEVICE, INSTALLED, _install
from test_journal_time_log_ui_stream_ingest import TIME_LOG_SOURCE_DEF

PLUGIN = "browser-history-plugin"
VISITS = "browser_visits"
ACTIVITY = "activity_events"
GROW_APP = "grow-app"
GROW_SOURCE = "grow_journal"
#: The app id the owner's node attested on 1 Oct, before the control plane named the Grow app.
PROVISIONAL = "grow-journal-app-unregistered"
JOURNAL = "journal_entries"
IDENTITY = ("writer_class", "writer_app_id", "writer_dataset_id")


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    """Every switch at its shipped default: unset."""
    monkeypatch.delenv(ACTIVITY_WRITER_FLAG, raising=False)
    monkeypatch.delenv(INCOGNITO_WITHHOLD_FLAG, raising=False)


@pytest.fixture()
def grow_source():
    """A journal source shaped like the Grow app's install: the time-log parser and mapper, its own id."""
    from topos.sources.runtime_install import install_source_definition

    definition = {**TIME_LOG_SOURCE_DEF, "source_id": GROW_SOURCE, "display_name": "Grow Journal",
                  "tables": [{**TIME_LOG_SOURCE_DEF["tables"][0], "table_id": "grow_journal_sessions",
                              "display_name": "Grow Journal Sessions"}]}
    handle = install_source_definition(definition)
    try:
        yield handle
    finally:
        handle.uninstall()


def _push(msg_id: str, source: str, record: Dict[str, Any], *, app: str, requester: str = OWNER,
          user: str = OWNER, dataset: str = AUTHORISED) -> Dict[str, Any]:
    """The relay message the control plane's app_ingest route sends (routes/ingestion.py)."""
    return {"id": msg_id, "type": "app_ingest",
            "payload": {"user_id": user, "dataset_id": dataset, "source_id": source, "records": [record],
                        "resource_id": f"dataset:{user}:{dataset}:{DEVICE}", "app_id": app,
                        "requesting_user_id": requester}}


def _captured(msg_id: str, source: str, record: Dict[str, Any], *, app: str, **push: Any) -> Dict[str, Any]:
    """Rule C at the control plane: the owner's own grant, the app listed for this source."""
    return _stamp(_push(msg_id, source, record, app=app, **push), cls=OWNER_APP, client_id=app, acting_user=OWNER)


def _visit(n: int = 1, **extra: Any) -> Dict[str, Any]:
    return {"url": f"https://example.test/articles/{n}", "title": "A synthetic page",
            "visited_at": f"2026-10-01T10:0{n}:00Z", **extra}


def _entry(n: int = 1) -> Dict[str, Any]:
    # The app's own session id: the same entry sent twice is one row.
    return {"id": f"grow-session-{n}", "startDate": "2026-10-01", "startTime": f"0{n}:00 AM", "endDate": "2026-10-01",
            "endTime": f"0{n}:30 AM", "duration": 30, "project": "Synthetic project", "goal": "A synthetic goal",
            "accomplished": f"A synthetic note {n}.", "completed": True, "location": "", "group": "Solo"}


def _identity(row: Dict[str, Any]) -> tuple:
    return tuple(row[name] for name in IDENTITY)


def _visits(db: sqlite3.Connection) -> List[Dict[str, Any]]:
    return [r for r in _rows(db, ACTIVITY) if r["source_id"] == VISITS]


def _proven_visits(db: sqlite3.Connection) -> frozenset:
    """What the browsing-interest family counts (interest_family.build: ``capture_receipts.proven_rows``)."""
    from topos.permissions_v2.interest_family import _receipt_row

    return cr.proven_rows(db, owner_id=OWNER, table=ACTIVITY, source_id=VISITS,
                          rows=[_receipt_row(r) for r in _visits(db)])


def _attest(db: sqlite3.Connection, table: str, source: str, app: str) -> Dict[str, Any]:
    preview = cr.preview(db, owner_id=OWNER, table=table, source_id=source, app_id=app)
    receipt = cr.attest(db, owner_id=OWNER, table=table, source_id=source, app_id=app,
                        preview_digest=preview["preview_digest"], confirm=True)
    db.commit()
    return receipt


def _eligible(db: sqlite3.Connection, table: str, source: str) -> int:
    return cr.preview(db, owner_id=OWNER, table=table, source_id=source, app_id="any-app")["row_count"]


# --- browser visits ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_plugins_visit_records_its_door_by_default_and_proves_once_the_plugin_is_attested(
        conn, captured_jobs):
    _install(conn, source=VISITS)
    assert (await _relay(_captured("req-v1", VISITS, _visit(1), app=PLUGIN)))["status"] == "ok"
    (row,) = _visits(conn)
    # The plugin attached under the resource's name for the store; the door records the install's.
    assert _identity(row) == ("owner_app", PLUGIN, INSTALLED)
    assert _proven_visits(conn) == frozenset()  # the owner has not attested the plugin on this node yet

    receipt = _attest(conn, ACTIVITY, VISITS, PLUGIN)
    assert receipt["row_count"] == 0  # nothing waits for a receipt: the visit proves through its door
    assert _proven_visits(conn) == {row["event_id"]}

    # Every later visit counts with no further step.
    assert (await _relay(_captured("req-v2", VISITS, _visit(2), app=PLUGIN)))["status"] == "ok"
    assert _proven_visits(conn) == {r["event_id"] for r in _visits(conn)} and len(_visits(conn)) == 2

    cr.revoke(conn, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    conn.commit()
    assert _proven_visits(conn) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("attested", ["browser-plugin", "some-other-app"])
async def test_a_visit_proves_nothing_under_an_app_the_owner_attested_by_another_id(conn, captured_jobs,
                                                                                       attested):
    _install(conn, source=VISITS)
    _attest(conn, ACTIVITY, VISITS, attested)
    assert (await _relay(_captured("req-v", VISITS, _visit(), app=PLUGIN)))["status"] == "ok"
    assert _proven_visits(conn) == frozenset()


@pytest.mark.asyncio
async def test_an_app_attested_for_the_plugins_other_source_proves_no_visit(conn, captured_jobs):
    _install(conn, source=VISITS)
    _install(conn, source="browser_events")
    _attest(conn, ACTIVITY, "browser_events", PLUGIN)
    assert (await _relay(_captured("req-v", VISITS, _visit(), app=PLUGIN)))["status"] == "ok"
    assert _proven_visits(conn) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", [GRANTEE, OWNER])
async def test_an_unstamped_visit_is_cp_relay_and_no_receipt_can_take_it(conn, captured_jobs, requester):
    """A grantee's push, or the owner's through an app the control plane does not list: nobody vouched."""
    _install(conn, source=VISITS)
    _attest(conn, ACTIVITY, VISITS, PLUGIN)
    assert (await _relay(_push("req-v", VISITS, _visit(), app=PLUGIN, requester=requester)))["status"] == "ok"
    (row,) = _visits(conn)
    assert _identity(row) == ("cp_relay", None, INSTALLED)
    assert _proven_visits(conn) == frozenset()
    assert _eligible(conn, ACTIVITY, VISITS) == 0


@pytest.mark.asyncio
async def test_a_grantees_rewrite_of_the_plugins_visit_is_refused(conn, captured_jobs):
    _install(conn, source=VISITS)
    _attest(conn, ACTIVITY, VISITS, PLUGIN)
    assert (await _relay(_captured("req-own", VISITS, _visit(title="The owner's page"), app=PLUGIN)))["status"] == "ok"
    result = await _relay(_push("req-forge", VISITS, _visit(title="A forged title"), app="grantee-app",
                                requester=GRANTEE))
    assert result["status"] == "error"
    (row,) = _visits(conn)
    assert row["title"] == "The owner's page" and _identity(row) == ("owner_app", PLUGIN, INSTALLED)
    assert _proven_visits(conn) == {row["event_id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("off", ["false", "0", "off", "no"])
async def test_switched_off_a_visit_records_no_writer_and_waits_for_a_receipt_as_before(
        conn, captured_jobs, monkeypatch, off):
    monkeypatch.setenv(ACTIVITY_WRITER_FLAG, off)
    _install(conn, source=VISITS)
    _attest(conn, ACTIVITY, VISITS, PLUGIN)
    assert (await _relay(_captured("req-v", VISITS, _visit(), app=PLUGIN)))["status"] == "ok"
    (row,) = _visits(conn)
    assert _identity(row) == (None, None, None)
    assert _proven_visits(conn) == frozenset()  # the attested plugin's own visit, unproven
    assert _eligible(conn, ACTIVITY, VISITS) == 1
    _attest(conn, ACTIVITY, VISITS, PLUGIN)  # only a receipt that lists it makes it count
    assert _proven_visits(conn) == {row["event_id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["another_owners_dataset", "no_install", "two_datasets"])
async def test_without_this_owners_one_install_a_visit_keeps_the_authorised_dataset_and_proves_nothing(
        conn, captured_jobs, case):
    push: Dict[str, Any] = {}
    if case == "another_owners_dataset":
        _install(conn, source=VISITS)
        push["dataset"] = f"{GRANTEE}:default:{DEVICE}"
    elif case == "two_datasets":
        _install(conn, source=VISITS)
        _install(conn, source=VISITS, dataset=f"{OWNER}:topos:topos_" + "8" * 32)
    assert (await _relay(_captured("req-" + case, VISITS, _visit(), app=PLUGIN, **push)))["status"] == "ok"
    (row,) = _visits(conn)
    assert row["writer_dataset_id"] == push.get("dataset", AUTHORISED)
    if case == "another_owners_dataset":
        _attest(conn, ACTIVITY, VISITS, PLUGIN)
    else:
        with pytest.raises(PolicyError):
            _attest(conn, ACTIVITY, VISITS, PLUGIN)
    assert _proven_visits(conn) == frozenset()


@pytest.mark.asyncio
async def test_the_plugins_page_events_record_the_plugin_too(conn, captured_jobs):
    _install(conn, source="browser_events")
    event = {"event_type": "highlight", "url": "https://example.test/articles/1", "title": "A synthetic page",
             "content": "a selected span", "visited_at": "2026-10-01T10:00:00Z"}
    assert (await _relay(_captured("req-e", "browser_events", event, app=PLUGIN)))["status"] == "ok"
    (row,) = [r for r in _rows(conn, ACTIVITY) if r["source_id"] == "browser_events"]
    assert _identity(row) == ("owner_app", PLUGIN, INSTALLED)


@pytest.mark.asyncio
async def test_a_private_window_visit_gets_no_canonical_row_by_default(conn, captured_jobs):
    _install(conn, source=VISITS)
    result = await _relay(_captured("req-private", VISITS, _visit(incognito=True), app=PLUGIN))
    assert result["status"] == "ok"  # accepted, so the plugin does not resend it
    assert _visits(conn) == []
    assert [tuple(r) for r in conn.execute("SELECT incognito FROM browser_visits").fetchall()] == [(1,)]


# --- journal entries pushed by the Grow app -----------------------------------------------------------

def _grow_rows(db: sqlite3.Connection) -> List[Dict[str, Any]]:
    return [r for r in _rows(db, JOURNAL) if r["source_id"] == GROW_SOURCE]


def _grow_proven(db: sqlite3.Connection, row: Dict[str, Any]) -> bool:
    return cr.proven(db, owner_id=OWNER, table=JOURNAL, identity_source_id=GROW_SOURCE, row=row)


def _pre_stamp(db: sqlite3.Connection, entry_id: str) -> None:
    db.execute(f"INSERT INTO {JOURNAL} (entry_id, entry_at, content, source_id) VALUES (?,?,?,?)",
               (entry_id, "2026-09-01T10:00:00", f"Synthetic older entry {entry_id}.", GROW_SOURCE))
    db.commit()


@pytest.mark.asyncio
async def test_a_grow_push_proves_under_grow_app_and_never_under_the_provisional_id(conn, captured_jobs,
                                                                                     grow_source):
    """The owner's node on 1 Oct: one receipt over the older rows, made under a provisional app id."""
    _install(conn, source=GROW_SOURCE)
    _pre_stamp(conn, "old-1")
    provisional = _attest(conn, JOURNAL, GROW_SOURCE, PROVISIONAL)
    assert provisional["row_count"] == 1

    assert (await _relay(_captured("req-g1", GROW_SOURCE, _entry(1), app=GROW_APP)))["status"] == "ok"
    old, new = sorted(_grow_rows(conn), key=lambda r: r["entry_id"] != "old-1")
    assert _identity(new) == ("owner_app", GROW_APP, INSTALLED)
    assert _grow_proven(conn, old) and not _grow_proven(conn, new)

    # The one owner step: attest the app under the id the control plane stamps. Nothing is left to list.
    real = _attest(conn, JOURNAL, GROW_SOURCE, GROW_APP)
    assert real["row_count"] == 0
    assert _grow_proven(conn, old) and _grow_proven(conn, new)
    assert (await _relay(_captured("req-g2", GROW_SOURCE, _entry(2), app=GROW_APP)))["status"] == "ok"
    assert all(_grow_proven(conn, r) for r in _grow_rows(conn)) and len(_grow_rows(conn)) == 3


@pytest.mark.asyncio
async def test_an_unstamped_grow_push_is_cp_relay_and_stays_unprovable(conn, captured_jobs, grow_source):
    """What every Grow push became while the control plane did not list the app."""
    _install(conn, source=GROW_SOURCE)
    _attest(conn, JOURNAL, GROW_SOURCE, GROW_APP)
    assert (await _relay(_push("req-g", GROW_SOURCE, _entry(), app=GROW_APP)))["status"] == "ok"
    (row,) = _grow_rows(conn)
    assert _identity(row) == ("cp_relay", None, INSTALLED)
    assert not _grow_proven(conn, row)
    assert _eligible(conn, JOURNAL, GROW_SOURCE) == 0  # never offered to a receipt: no backfill


@pytest.mark.asyncio
async def test_the_grow_app_re_sending_an_entry_takes_its_cp_relay_row_over(conn, captured_jobs, grow_source):
    """The only road back for a cp_relay entry: the app itself sends it again, stamped."""
    _install(conn, source=GROW_SOURCE)
    _attest(conn, JOURNAL, GROW_SOURCE, GROW_APP)
    assert (await _relay(_push("req-first", GROW_SOURCE, _entry(), app=GROW_APP)))["status"] == "ok"
    assert (await _relay(_captured("req-again", GROW_SOURCE, _entry(), app=GROW_APP)))["status"] == "ok"
    (row,) = _grow_rows(conn)
    assert _identity(row) == ("owner_app", GROW_APP, INSTALLED)
    assert _grow_proven(conn, row)


# --- file imports: the Grow data file and a ChatGPT export --------------------------------------------

def _import_message(msg_id: str, source: str, schema: str, file_format: str, body: bytes, *,
                    dataset: str = AUTHORISED) -> Dict[str, Any]:
    """The relay message the control plane's upload routes send (start_ingestion)."""
    import base64

    return {"id": msg_id, "type": "start_ingestion",
            "payload": {"dataset_id": dataset, "job_id": f"job-{msg_id}", "source_id": source, "schema_id": schema,
                        "file_format": file_format, "file_base64": base64.b64encode(body).decode()}}


def _owner_import(message: Dict[str, Any]) -> Dict[str, Any]:
    """Rule A at the control plane: the owner's own first-party session started the upload."""
    return _stamp(message, cls=OWNER_APP, acting_user=OWNER)


JOURNAL_FILE = "demo_journal_file"
JOURNAL_CSV = b"entry_id,entry_at,content\nj-1,2026-09-01T10:00:00,A synthetic entry.\n"
EXPORT = "chatgpt_file_ingestion"


def _export_body() -> bytes:
    return (_export_line("m-user", "A synthetic prompt.") + "\n"
            + _export_line("m-reply", "A synthetic reply.", role="assistant") + "\n").encode()


def _export_row(db: sqlite3.Connection, message_id: str = "m-user") -> Dict[str, Any]:
    (row,) = [dict(r) for r in db.execute("SELECT * FROM ai_chat_messages WHERE message_id LIKE ?",
                                          (f"%{message_id}",)).fetchall()]
    return row


@pytest.mark.asyncio
async def test_the_owners_journal_import_is_written_through_the_install_and_proves_at_its_door(
        conn, captured_jobs, tmp_path, monkeypatch):
    _install(conn, source=JOURNAL_FILE)
    message = _owner_import(_import_message("req-j", JOURNAL_FILE, "demo.journal.v1", "csv", JOURNAL_CSV))
    await _start_ingestion(message, captured_jobs, conn, tmp_path, monkeypatch)
    (row,) = [r for r in _rows(conn, JOURNAL) if r["source_id"] == JOURNAL_FILE]
    assert _identity(row) == ("owner_import", None, INSTALLED)
    assert cr.proven(conn, owner_id=OWNER, table=JOURNAL, identity_source_id=JOURNAL_FILE, row=row)
    assert _eligible(conn, JOURNAL, JOURNAL_FILE) == 0  # nothing waits for a receipt


@pytest.mark.asyncio
async def test_the_owners_export_import_is_written_through_the_install_and_proves_at_its_door(
        conn, captured_jobs, tmp_path, monkeypatch):
    from topos.permissions_v2 import ai_chat_capture

    _install(conn, source=EXPORT)
    message = _owner_import(_import_message("req-x", EXPORT, "chatgpt.conversation.v2", "jsonl", _export_body()))
    await _start_ingestion(message, captured_jobs, conn, tmp_path, monkeypatch)
    prompt, reply = _export_row(conn, "m-user"), _export_row(conn, "m-reply")
    assert _identity(prompt) == ("owner_import", None, INSTALLED) == _identity(reply)
    assert ai_chat_capture.capture_proven(conn, owner_id=OWNER, identity_source_id=EXPORT, row=prompt)
    assert not ai_chat_capture.capture_proven(conn, owner_id=OWNER, identity_source_id=EXPORT, row=reply)
    assert ai_chat_capture.certified_dataset(conn, owner_id=OWNER, row=prompt) == INSTALLED


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unstamped", "another_owners_dataset", "two_installs", "no_install"])
async def test_an_import_the_install_cannot_bind_or_no_owner_door_started_proves_nothing(
        conn, captured_jobs, tmp_path, monkeypatch, case):
    """``two_installs`` is the owner's export source on 1 Oct: two live installs on two datasets."""
    from topos.permissions_v2 import ai_chat_capture

    dataset = AUTHORISED
    if case == "another_owners_dataset":
        _install(conn, source=EXPORT)
        dataset = f"{GRANTEE}:default:{DEVICE}"
    elif case == "two_installs":
        _install(conn, source=EXPORT)
        _install(conn, source=EXPORT, dataset=f"{OWNER}:topos:topos_" + "8" * 32)
    elif case == "unstamped":
        _install(conn, source=EXPORT)
    message = _import_message("req-" + case, EXPORT, "chatgpt.conversation.v2", "jsonl", _export_body(),
                              dataset=dataset)
    if case != "unstamped":
        message = _owner_import(message)
    await _start_ingestion(message, captured_jobs, conn, tmp_path, monkeypatch)
    prompt = _export_row(conn)
    if case == "unstamped":
        assert _identity(prompt) == ("cp_relay", None, INSTALLED)  # the name of the store, never the writer
    else:
        assert _identity(prompt) == ("owner_import", None, dataset)
    assert not ai_chat_capture.capture_proven(conn, owner_id=OWNER, identity_source_id=EXPORT, row=prompt)


def test_only_sources_proven_against_their_install_are_bound():
    from topos.sources.registry import REGISTRY

    bound = {source for source in ("demo_journal_file", "browser_visits", "browser_events", EXPORT,
                                   "chatgpt_ui_conversation", "imessage", "signal", "notion_pages", "github_activity")
             if cr.bound_to_install(REGISTRY.get(source))}
    assert bound == {"demo_journal_file", "browser_visits", "browser_events", EXPORT, "github_activity"}
    assert not cr.bound_to_install(None)
