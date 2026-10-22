"""Fix 192 (DR-34, widens DR-31): an edit to one shared line's own stored row withholds that line, not the whole share.

22 Oct, live: five times a single re-captured AI-chat line (its own stored row rewritten: an edit, a whitespace
re-capture, a metadata-only re-pointing) took a recipient's whole share index away 11-90 s after the rewrite. Under
NR188 the line's own-row dependency was already found moved (`row_revision`) and withheld, and then its fingerprint,
which is that same row's revision, failed and dropped every member.

Behind `TOPOS_PERMISSIONS_V2_INDEX_NARROW_OWN_ROW_EDITS` (off by default, read only while INDEX_NARROW_MEMBERS is on):

- a message line whose fingerprint is its own row alone (no witness facts, not a projection, interest or journal row),
  whose own key moved as `row_revision`, stays withheld (`fingerprint: own_row_edit`, said once per check); the sweep
  publishes the index without it, which owes one rebuild, and the line returns only through that rebuild after a
  fresh review of its new text;
- every restriction still drops the whole index at once: a delete or close (no fingerprint), the keep-from-every-share
  flag, a row count other than one, an NSFW re-flag, an emptied or blank row, an Off-limits veto, a broken native
  proof (iMessage), a lineage or context move, and everything with either switch off;
- the file a narrowed publish replaces is zero-overwritten.

The share: the NR188 AI-chat share (an imported ChatGPT export under an active Off-limits boundary) with a second chat
holding one more owner line, so the rest of the share can be seen to keep serving. Every text is invented.
"""
from __future__ import annotations

import logging
import os
import sqlite3

import pytest

from tests.ingestion.test_ai_chat_writer_class import captured_jobs  # noqa: F401 (fixture)
from tests.permissions_v2.test_fresh_node_message_tables import (  # noqa: F401 (fixture)
    EXPORT, LINE, NOW as AI_NOW, OWNER as AI_OWNER, _bound, _own_pass, _share_policy, ai_chat_only)
from tests.permissions_v2.test_nr188_narrow_member_drop import (  # noqa: F401 (fixtures)
    NARROW, _ai, _grant, _members, _path, _sweep, _write, events, node)
from tests.permissions_v2.test_refresh_loop import Clock, loop_for
from topos.permissions_v2 import search_index, switches
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.search_index import SearchIndexService, _file_state

OWN = "TOPOS_PERMISSIONS_V2_INDEX_NARROW_OWN_ROW_EDITS"
OTHER = "Notes on the tide pools field trip for the volunteer group, invented for this test."
EDITED = "I am sketching a talk about tide pools for the autumn open day at work."
QUERY = "tide pools talk field trip"
DRIFT_OWN = "message search index member drift (dependencies: row_revision)"
DRIFT_FINGERPRINT = "message search index member drift (fingerprint: own_row_edit)"


# --- the share ----------------------------------------------------------------------------------------------------

def _copy(db, table, key, value, **changes):
    """Insert a copy of one row of `table` (its `key` = `value`) with `changes` applied."""
    cursor = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,))
    row = dict(zip([column[0] for column in cursor.description], cursor.fetchone()))
    row.update(changes)
    db.execute(f"INSERT INTO {table}({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))


@pytest.fixture
def ai_share_two(ai_chat_only):
    """The NR188 AI-chat share with a second chat holding one more assessed owner line, 'm-other'."""
    from tests.permissions_v2.message_search_harness import Node
    from tests.permissions_v2.test_ingest_provenance import owner as owner_as
    from types import SimpleNamespace
    db = ai_chat_only.db
    db.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
               "VALUES('protected-entity','person','Mara Example','mara example')")
    db.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
               "VALUES('bh','protected-entity','Mara Example','mara example','complete')")
    # The second chat as the same import door wrote the first (its writer class and batch, so its source's posture
    # and proof are the first chat's): copies of the first chat's rows with their own ids, time and text.
    _copy(db, "ai_chat_conversations", "conversation_id", "chatgpt:thread-file", conversation_id="chatgpt:thread-other",
          source_record_id="thread-other", title="an invented second chat", latest_title=None,
          created_at="2026-09-01T09:30:00Z", updated_at="2026-09-01T09:30:00Z")
    _copy(db, "ai_chat_messages", "message_id", "m-line", message_id="m-other", conversation_id="chatgpt:thread-other",
          source_record_id="m-other", event_at="2026-09-01T09:30:00Z", content=OTHER, sequence=0,
          metadata_json='{"thread_id": "thread-other", "original_source": "chatgpt"}')
    db.commit()
    resolver, reviews = _bound(ai_chat_only)
    result = _own_pass(resolver, reviews)
    assert result["assessed"] == 2, result
    share = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), ai_chat_only.root / "share",
                 model=None, search_raw=_share_policy(), now=AI_NOW)
    with owner_as(actor=AI_OWNER):
        assert share.index.rebuild("grant-search", now=AI_NOW) == {"state": "ready", "member_count": 2}
    share.db = db
    share.store = ai_chat_only
    return share


@pytest.fixture
def both(monkeypatch):
    monkeypatch.setenv(NARROW, "true")
    monkeypatch.setenv(OWN, "true")


def _rebuild(share, *, review=False):
    from tests.permissions_v2.test_ingest_provenance import owner as owner_as
    if review:
        resolver, reviews = share.corpus.resolver, share.corpus.reviews
        _own_pass(resolver, reviews)
    with owner_as(actor=AI_OWNER):
        return share.index.rebuild("grant-search", now=AI_NOW)


def _by_record(share) -> dict:
    """record_id -> opaque id of every member of the share's index file now."""
    key = share.index.keys.get(_grant(share), create=False)
    opened = share.index._open(_path(share))
    return {search_index.unseal(key, opaque, sealed)["record_id"]: opaque for opaque, sealed in opened["sealed"]}


def _served(share) -> set:
    output, refused = share.search_request(QUERY, k=10)
    assert refused is None, refused
    return {record["content"] for record in output["records"]}


def _authority(share):
    with share.ledger._transaction() as db:
        return share.ledger._authority(db, _grant(share), share.now[0])[0]


def _edit(share, how="edit", message_id="m-line"):
    if how == "edit":
        _ai(share, "UPDATE ai_chat_messages SET content=? WHERE message_id=?", (EDITED, message_id))
    elif how == "metadata_only":
        _ai(share, "UPDATE ai_chat_messages SET metadata_json='{\"thread_id\":\"other\"}' WHERE message_id=?",
            (message_id,))
    elif how == "whitespace_only":
        _ai(share, "UPDATE ai_chat_messages SET content=content||' ' WHERE message_id=?", (message_id,))
    else:
        raise AssertionError(how)


def _gone(share, lines, *, admitted_first=False) -> list:
    """The whole index went in this sweep, and no narrowed copy was published. `admitted_first`: a check that met the
    edited line before the failure (the members' order is their opaque ids') may have said its fingerprint line."""
    assert not _path(share).exists()
    assert not any(line.startswith("message search index narrowed") for line in lines)
    if not admitted_first:
        assert DRIFT_FINGERPRINT not in lines
    return lines


def _member(share, record_id="m-line") -> dict:
    key = share.index.keys.get(_grant(share), create=False)
    opened = share.index._open(_path(share))
    return next(search_index.unseal(key, opaque, sealed) for opaque, sealed in opened["sealed"]
                if search_index.unseal(key, opaque, sealed)["record_id"] == record_id)


def _case(share, member):
    """The canonical own-row edit, read as `_members_current` reads it: (rows, facts, fingerprint, why)."""
    with share.corpus.resolver._read(gated=False) as (conn, _floor):
        conn.row_factory = sqlite3.Row
        rows, facts = search_index._live_rows(conn, member)
        rows = [dict(row) for row in rows]
    fingerprint = search_index._member_fingerprint(rows, facts, table=member["table"])
    return rows, facts, fingerprint, {"moved": {share.index._own_key(member): "row_revision"}}


# --- the fix: one line withheld, the rest serving, one rebuild owed ---------------------------------------------------

@pytest.mark.parametrize("how", ["edit", "metadata_only", "whitespace_only"])
def test_an_own_row_edit_withholds_the_line_keeps_the_index_narrowed_and_owes_a_rebuild(ai_share_two, caplog, both,
                                                                                         how):
    share = ai_share_two
    other = _by_record(share)["m-other"]
    clock = Clock(share.now[0])
    loop = loop_for(share, clock)
    loop.observe(share.index)
    _edit(share, how)
    assert _sweep(share, caplog) == [DRIFT_OWN, DRIFT_FINGERPRINT, "message search index narrowed (1 withheld)"]
    assert _path(share).exists() and search_index._narrowed_file(_path(share))
    assert _members(share) == {other}
    served = _served(share)
    assert OTHER in served and not served & {LINE, EDITED, LINE + " "}
    loop.observe(share.index)
    assert loop._pending[_grant(share)]["causes"] == {"members_narrowed"}
    again = SearchIndexService(ledger=share.ledger, resolver=share.corpus.resolver, reviews=share.corpus.reviews,
                               root=share.index.root, embedding_model=lambda: None)
    assert again.narrowed_files() == {_path(share).name}          # a restart still owes the rebuild


def test_the_withheld_line_is_never_returned_before_or_after_the_narrowing(ai_share_two, caplog, both, monkeypatch):
    share = ai_share_two
    line = _by_record(share)["m-line"]
    _edit(share)
    state = _file_state(_path(share))
    output, refused = share.search_request(QUERY, k=10)
    assert output is None and refused is not None                 # before the sweep: refused, as not served now
    assert _file_state(_path(share)) == state                     # and never shredded on the request path
    with pytest.raises(PolicyError, match="search_index_stale"):
        share.index.check_own(_grant(share), _authority(share), now=share.now[0], released=frozenset({line}))
    assert _file_state(_path(share)) == state
    assert _sweep(share, caplog)[-1] == "message search index narrowed (1 withheld)"
    with pytest.raises(PolicyError, match="search_index_stale"):
        share.index.check_own(_grant(share), _authority(share), now=share.now[0], released=frozenset({line}))
    passed = []
    original = SearchIndexService._current

    def current(self, path, *args, **kwargs):
        verdict = original(self, path, *args, **kwargs)
        if verdict and not kwargs.get("refresh") and not kwargs.get("narrowed"):
            passed.append({opaque for opaque, _sealed in self._open(path)["sealed"]})
        return verdict
    monkeypatch.setattr(SearchIndexService, "_current", current)
    for _ in range(3):
        output, refused = share.search_request(QUERY, k=10)
        assert refused is None
        assert all(record["content"] not in (LINE, EDITED) for record in output["records"])
    assert passed and all(line not in held for held in passed)    # no passing check ever read a file holding it


@pytest.mark.parametrize("switches_on", [True, False], ids=["switches_on", "both_off"])
@pytest.mark.parametrize("how", ["update_same_values", "insert_or_replace_same_row"])
def test_a_byte_identical_rewrite_is_not_drift(ai_share_two, caplog, monkeypatch, how, switches_on):
    share = ai_share_two
    monkeypatch.setenv(NARROW, "true" if switches_on else "false")
    monkeypatch.setenv(OWN, "true" if switches_on else "false")
    state = _file_state(_path(share))
    if how == "update_same_values":
        _ai(share, "UPDATE ai_chat_messages SET content=content, metadata_json=metadata_json, sequence=sequence "
                   "WHERE message_id='m-line'")
    else:
        _ai(share, "INSERT OR REPLACE INTO ai_chat_messages SELECT * FROM ai_chat_messages WHERE message_id='m-line'")
    assert _sweep(share, caplog) == []
    assert _file_state(_path(share)) == state and not search_index._narrowed_file(_path(share))
    assert set(_by_record(share)) == {"m-line", "m-other"}
    assert _served(share) == {LINE, OTHER}


# --- either switch off: 1.5.15 ------------------------------------------------------------------------------------

@pytest.mark.parametrize("path_taken", ["sweep", "request"])
def test_with_narrowing_off_an_own_row_edit_drops_the_whole_index_as_before(ai_share_two, caplog, monkeypatch,
                                                                            path_taken):
    share = ai_share_two
    monkeypatch.setenv(NARROW, "false")
    monkeypatch.setenv(OWN, "true")
    _edit(share)
    if path_taken == "sweep":
        assert _sweep(share, caplog) == ["message search index stale (dependencies)", DRIFT_OWN]
    else:
        output, refused = share.search_request(QUERY, k=10)
        assert output is None and refused is not None
    assert not _path(share).exists()


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
def test_with_the_own_row_switch_off_an_edit_drops_the_whole_index(ai_share_two, caplog, monkeypatch, bound):
    share = ai_share_two
    monkeypatch.setenv(NARROW, "true")
    monkeypatch.delenv(OWN, raising=False)
    monkeypatch.setattr(switches, "is_bound", lambda env=None: bound)
    assert switches.on(switches.INDEX_NARROW_OWN_ROW_EDITS) is False
    _edit(share)
    assert _sweep(share, caplog) == [DRIFT_OWN, "message search index stale (fingerprint)"]
    assert not _path(share).exists()


# --- restrictions still take the whole share at once -------------------------------------------------------------

def _columns(share) -> set:
    return {row[1] for row in share.db.execute("PRAGMA table_info(ai_chat_messages)")}


@pytest.mark.parametrize("change", ["delete", "deleted_at", "valid_to", "is_deleted", "share_excluded"])
def test_a_deleted_closed_or_kept_back_line_drops_the_whole_index(ai_share_two, caplog, both, change, events):
    share = ai_share_two
    if change == "delete":
        _ai(share, "DELETE FROM ai_chat_messages WHERE message_id='m-line'")
    elif change == "share_excluded":
        assert "share_excluded" in _columns(share)       # migration 85's column, 0 on every row as built
        _ai(share, "UPDATE ai_chat_messages SET share_excluded=1 WHERE message_id='m-line'")
    else:
        if change not in _columns(share):
            _ai(share, f"ALTER TABLE ai_chat_messages ADD COLUMN {change} TEXT")
        _ai(share, f"UPDATE ai_chat_messages SET {change}='2026-09-02T00:00:00Z' WHERE message_id='m-line'")
    events.clear()
    lines = _gone(share, _sweep(share, caplog))
    # Measured: each of these fails the line's own-row dependency load (the resolver refuses a gone, closed or kept
    # back row), before the fingerprint step is reached; the fingerprint's own None for a closed row is pinned on
    # `_own_row_edit` itself below.
    assert lines == ["message search index stale (member_unavailable)"]
    assert [fields.get("reason") for stage, fields in events if stage == "index_stale"] == ["member_unavailable"]


def test_a_row_count_other_than_one_drops_the_whole_index(ai_share_two, caplog, both, monkeypatch):
    share = ai_share_two
    _edit(share)
    original = search_index._live_rows

    def twice(conn, member):
        rows, facts = original(conn, member)
        return (rows + rows if member["record_id"] == "m-line" else rows), facts
    monkeypatch.setattr(search_index, "_live_rows", twice)
    lines = _gone(share, _sweep(share, caplog))
    assert lines[-2:] == ["message search index stale (classification_context)",
                          "message search index member drift (classification_context: row_unavailable)"]


def test_own_row_edit_refuses_a_row_count_other_than_one(ai_share_two, both):
    share = ai_share_two
    _edit(share)
    member = _member(share)
    rows, facts, fingerprint, why = _case(share, member)
    assert share.index._own_row_edit(member, rows, facts, fingerprint, "row_revision", why) is True
    assert share.index._own_row_edit(member, [], facts, fingerprint, "row_revision", why) is False
    assert share.index._own_row_edit(member, rows + rows, facts, fingerprint, "row_revision", why) is False


def test_an_nsfw_reflag_of_a_line_still_drops_the_whole_index(ai_share_two, caplog, both):
    share = ai_share_two
    _ai(share, "UPDATE ai_chat_messages SET content_nsfw=1 WHERE message_id='m-line'")
    assert _gone(share, _sweep(share, caplog)) == [DRIFT_OWN, "message search index stale (fingerprint)"]


@pytest.mark.parametrize("content", ["", "   "], ids=["empty", "blank"])
def test_an_emptied_line_drops_the_whole_index(ai_share_two, caplog, both, content):
    share = ai_share_two
    _ai(share, "UPDATE ai_chat_messages SET content=? WHERE message_id='m-line'", (content,))
    assert _gone(share, _sweep(share, caplog))[-1] == "message search index stale (fingerprint)"


def test_an_edit_that_names_an_off_limits_person_drops_the_whole_index(ai_share_two, caplog, both):
    share = ai_share_two
    _ai(share, "UPDATE ai_chat_messages SET content='I asked Mara Example about the tide pools talk.' "
               "WHERE message_id='m-line'")
    assert _gone(share, _sweep(share, caplog))[-1] == "message search index stale (member_unavailable)"


def test_an_edited_line_whose_lineage_moved_drops_the_whole_index(ai_share_two, caplog, both):
    share = ai_share_two
    _ai(share, "UPDATE ai_chat_messages SET content=? WHERE message_id='m-line'", (OTHER,))
    # Lineage runs after the fingerprint step: an edited line met first is admitted there, and then fails its own
    # lineage (its copy count moved), which takes the whole index all the same.
    assert _gone(share, _sweep(share, caplog), admitted_first=True)[-1] == "message search index stale (lineage)"


def test_an_edited_line_whose_entity_context_moved_otherwise_drops_the_whole_index(ai_share_two, caplog, both,
                                                                                   monkeypatch):
    from topos.permissions_v2.entity_boundary import EntityBoundary
    share = ai_share_two
    original = EntityBoundary.check

    def check(self, **kwargs):
        revision = original(self, **kwargs)
        loaded = any(name.startswith("_p2b") for name in kwargs["row"])
        return revision if loaded or kwargs["record_id"] != "m-line" else "read-otherwise"
    monkeypatch.setattr(EntityBoundary, "check", check)
    _edit(share)
    assert _gone(share, _sweep(share, caplog))[-1] == "message search index stale (context)"


# --- what `_own_row_edit` admits, condition by condition -----------------------------------------------------------

def test_a_projection_member_is_not_forgiven(ai_share_two, both):
    share = ai_share_two
    _edit(share)
    member = _member(share)
    rows, facts, fingerprint, why = _case(share, member)
    assert share.index._own_row_edit(member, rows, facts, fingerprint, "row_revision", why) is True
    projected = {**member, "projection": {"table": "signal_objects", "record_id": "p", "revision": "r"}}
    assert share.index._own_row_edit(projected, rows, facts, fingerprint, "row_revision", why) is False


def test_a_member_with_witness_facts_or_an_interest_or_a_journal_row_is_not_forgiven(ai_share_two, monkeypatch):
    share = ai_share_two
    monkeypatch.setenv(NARROW, "true")
    monkeypatch.setenv(OWN, "true")
    _edit(share)
    member = _member(share)
    rows, facts, fingerprint, why = _case(share, member)
    own = share.index._own_key(member)
    edit = share.index._own_row_edit
    assert edit(member, rows, facts, fingerprint, "row_revision", why) is True             # the canonical case
    assert edit({**member, "facts": ["f"]}, rows, facts, fingerprint, "row_revision", why) is False
    assert edit(member, rows, [[{"object_id": "f"}]], fingerprint, "row_revision", why) is False
    assert edit({**member, "interest": {"kind": "x"}}, rows, facts, fingerprint, "row_revision", why) is False
    journal = {**member, "table": "journal_entries"}
    journal_why = {"moved": {share.index._own_key(journal): "row_revision"}}   # its own key, so only the table refuses
    assert journal_why != why
    assert edit(journal, rows, facts, fingerprint, "row_revision", journal_why) is False
    for word in (None, "row_binding", "conversation_context", "missing"):
        assert edit(member, rows, facts, fingerprint, word, why) is False, word
    assert edit(member, rows, facts, None, "row_revision", why) is False                    # no fingerprint
    assert edit(member, rows, facts, fingerprint, "row_revision", {"moved": {"another-row": "row_revision"}}) is False
    assert edit(member, rows, facts, fingerprint, "row_revision", {}) is False
    assert edit(member, rows, facts, fingerprint, "row_revision", {"moved": {own: "conversation_context"}}) is False
    assert edit(member, [{**rows[0], "content_nsfw": 1}], facts, fingerprint, "row_revision", why) is False
    assert edit(member, [{**rows[0], "content": "  "}], facts, fingerprint, "row_revision", why) is False
    assert edit(member, [{**rows[0], "content": None}], facts, fingerprint, "row_revision", why) is False
    assert edit({"table": "ai_chat_messages"}, rows, facts, fingerprint, "row_revision", why) is False   # unparseable
    monkeypatch.setenv(OWN, "false")
    assert edit(member, rows, facts, fingerprint, "row_revision", why) is False             # the switch off


# --- iMessage: the native proof breaks first ---------------------------------------------------------------------

@pytest.mark.parametrize("change", ["edit", "scrub", "reflag"])
def test_an_imessage_edit_still_drops_the_whole_index_with_both_switches_on(node, caplog, both, change):
    if change == "edit":
        _write(node, "UPDATE conversation_messages SET content='garden watering chore item5 edited' "
                     "WHERE message_id='imessage:5'")
    elif change == "scrub":
        _write(node, "UPDATE conversation_messages SET content='' WHERE message_id='imessage:5'")
    else:
        _write(node, "ALTER TABLE conversation_messages ADD COLUMN content_nsfw INTEGER")
        _write(node, "UPDATE conversation_messages SET content_nsfw=1 WHERE message_id='imessage:5'")
    lines = _sweep(node, caplog)
    assert lines[-1] == "message search index stale (member_unavailable)", lines
    assert not _path(node).exists() and DRIFT_FINGERPRINT not in lines


# --- attribution, the replaced file, the rebuild ------------------------------------------------------------------

def test_only_the_first_own_row_edit_of_a_check_is_said(ai_share_two, caplog, both, events):
    share = ai_share_two
    _edit(share, message_id="m-line")
    _ai(share, "UPDATE ai_chat_messages SET content=? WHERE message_id='m-other'",
        ("An invented edit of the field trip note about tide pools.",))
    events.clear()
    lines = _sweep(share, caplog)
    assert lines.count(DRIFT_FINGERPRINT) == 1 and lines[-1] == "message search index narrowed (2 withheld)"
    assert [fields for stage, fields in events if stage == "index_stale" and fields.get("reason") == "fingerprint"] \
        == [{"reason": "fingerprint", "sub": "own_row_edit", "scope": "member"}]
    assert _members(share) == set()


def test_the_file_a_narrowed_publish_replaces_is_zeroed(ai_share_two, caplog, both):
    share = ai_share_two
    other = _by_record(share)["m-other"]
    fd = os.open(_path(share), os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        assert size > 0 and os.pread(fd, size, 0).count(0) < size     # a real index file to begin with
        _edit(share)
        assert _sweep(share, caplog)[-1] == "message search index narrowed (1 withheld)"
        assert os.fstat(fd).st_nlink == 0                               # the replaced inode, unlinked
        assert os.pread(fd, size, 0) == b"\0" * size                    # and zeroed
    finally:
        os.close(fd)
    assert _members(share) == {other}


def test_zero_open_never_raises_and_closes_the_handle(tmp_path, caplog):
    path = tmp_path / "f"
    path.write_bytes(b"x" * (search_index._ZERO_CHUNK + 3))
    handle = open(path, "r+b", buffering=0)
    search_index._zero_open(handle)
    assert handle.closed and path.read_bytes() == b"\0" * (search_index._ZERO_CHUNK + 3)
    handle = open(path, "rb", buffering=0)                      # cannot be written: logged, closed, never raised
    with caplog.at_level(logging.WARNING):
        search_index._zero_open(handle)
    assert handle.closed
    assert any("zeroing unavailable" in record.getMessage() for record in caplog.records)


def test_the_rebuild_owed_brings_the_edited_line_back_only_with_a_review_of_its_new_text(ai_share_two, caplog, both):
    share = ai_share_two
    _edit(share)
    assert _sweep(share, caplog)[-1] == "message search index narrowed (1 withheld)"
    assert _rebuild(share)["state"] == "ready"
    assert set(_by_record(share)) == {"m-other"}                  # not re-qualified without a review of its new text
    assert _rebuild(share, review=True) == {"state": "ready", "member_count": 2}
    assert set(_by_record(share)) == {"m-line", "m-other"}
    assert _served(share) == {EDITED, OTHER}
    assert not search_index._narrowed_file(_path(share)) and share.index.narrowed_files() == set()
    assert share.index.take_narrowed() == set()
