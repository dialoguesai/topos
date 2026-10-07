"""Review R1 (node), R-M5 and R-L4: what the Off-limits store keeps of an entry's identifiers, and `add_aliases`.

protects: an entry can now say which of its aliases are a handle, a username or an id (`identifier_aliases_json`), so
the share boundary reads those as handles and never as names. The store is the one writer. These tests hold:
  - identifiers stay among the aliases (every older reader keeps matching them) and are listed apart;
  - a name always wins: a value that is a name of the entry, given now or held before, is never listed, so the list
    can never make the boundary stop reading a real name as one;
  - the column is added in place by the first write that needs it and by no other, and a database without it reads
    and writes as before;
  - `add_aliases` gives an existing entry names and identifiers and nothing else: the owner's tier and note stay, an
    entry that gains nothing is not written, and an entry whose clean-up had finished waits again with a notice.
Every person, handle and id here is invented.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.lifecycle.blackhole import OWNER, IDENTIFIERS_COLUMN, BlackholeStore, has_identifier_aliases
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.protection_clock import TOMBSTONES_SQL
from topos.storage.canonical import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

NAME = "Quorra Vellaby"
EMAIL = "q.vellaby@mail.example"
CONTACT = "owner-1:default:contact:7471fce8530d7bd0"


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "canonical.db"), check_same_thread=False)
    apply_all_migrations(c)
    c.execute(TOMBSTONES_SQL)
    ConversationsTablesManager(c).ensure_tables()
    yield c
    c.close()


def _row(c, name=NAME.lower()):
    columns = [row[1] for row in c.execute("PRAGMA table_info(entity_blackholes)")]
    return dict(zip(columns, c.execute("SELECT * FROM entity_blackholes WHERE normalized_name=?", (name,)).fetchone()))


def test_a_hand_made_entry_adds_no_column_and_lists_nothing(conn):
    record = BlackholeStore(conn).blackhole_entity(entity_ref=NAME)
    assert not has_identifier_aliases(conn)
    assert record["identifier_aliases"] == [] and BlackholeStore(conn).list()[0]["identifier_aliases"] == []


def test_identifiers_stay_among_the_aliases_and_are_listed_apart(conn):
    store = BlackholeStore(conn)
    record = store.blackhole_entity(entity_ref=NAME, aliases=["Quorra", "Q. Vellaby"], identifiers=[EMAIL, CONTACT, "hopewell"])
    listed = {EMAIL, "owner-1 default contact 7471fce8530d7bd0", "hopewell"}
    assert set(record["identifier_aliases"]) == listed
    assert listed | {"quorra", "q. vellaby"} == set(record["aliases"])
    assert json.loads(_row(conn)[IDENTIFIERS_COLUMN]) == sorted(listed)
    boundary = EntityBoundary(conn)
    assert boundary.name_parts == {"quorra", "vellaby"}                    # the id's and the address's words: none


def test_a_name_always_wins(conn):
    """Rule: `BlackholeStore._marked` never lists a value that is a name of the entry. Drop the subtraction and the
    entry's own name and its name aliases are read as handles: their parts and forms stop withholding."""
    store = BlackholeStore(conn)
    record = store.blackhole_entity(entity_ref="Hope Vellaby", aliases=["Hope", "Hope Vellaby"],
                                    identifiers=["hope", "Hope Vellaby", EMAIL])
    assert record["identifier_aliases"] == [EMAIL]
    boundary = EntityBoundary(conn)
    assert {"hope", "vellaby"} <= boundary.name_parts
    # ... and the same when the identifier arrives later, onto an entry that holds the name
    store.add_aliases(entity_ref="Hope Vellaby", identifiers=["hope", "hope vellaby", "hopewell"])
    assert set(store.get("Hope Vellaby")["identifier_aliases"]) == {EMAIL, "hopewell"}
    # ... and a name given later takes a value off the list
    store.add_aliases(entity_ref="Hope Vellaby", aliases=["hopewell"])
    assert store.get("Hope Vellaby")["identifier_aliases"] == [EMAIL]
    assert "hopewell" in EntityBoundary(conn).name_parts


def test_an_entry_named_by_an_identifier_lists_its_own_name(conn):
    """A contact with no usable name is carried under a handle: there is no name to win."""
    record = BlackholeStore(conn).blackhole_entity(entity_ref=EMAIL, identifiers=[EMAIL, CONTACT])
    assert EMAIL in record["identifier_aliases"]
    assert EntityBoundary(conn).name_parts == set()


def test_add_aliases_keeps_the_owners_tier_and_note(conn):
    """R-L4. Rule: `add_aliases` writes aliases and identifiers only. Route it through `blackhole_entity` and the
    owner's stricter tier is reset to the default and the note replaced."""
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=NAME, processing_tier="local_only", note="the owner's own")
    result = store.add_aliases(entity_ref=NAME, aliases=["Quorra"], identifiers=[EMAIL])
    assert result["grew"]
    row = _row(conn)
    assert (row["processing_tier"], row["note"], row["rebuild_state"]) == ("local_only", "the owner's own", "pending")
    assert set(json.loads(row["aliases_json"])) == {"quorra", EMAIL}


def test_add_aliases_that_adds_nothing_writes_nothing(conn):
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=NAME, aliases=["Quorra"], identifiers=[EMAIL])
    before, changes = _row(conn), conn.total_changes
    result = store.add_aliases(entity_ref=NAME, aliases=["quorra"], identifiers=[EMAIL])
    assert not result["grew"]
    assert _row(conn) == before and conn.total_changes == changes          # not even the timestamp: no clock tick


def test_an_entry_whose_clean_up_had_finished_stays_finished_and_its_new_names_wait(conn):
    """R-L4, as the third fix round rules it. Until then the entry was put back to `pending` with a notice, and
    `pending` withholds every summary from the owner's own outside client: a change to an owner-serving path made
    by an unattended step. Now the entry stays as it was and what it gains is carried and waiting: every reader
    that can answer another person sees the new identifier at once, the owner's own tools do not, and "fully
    hidden" is still said because for the names the entry had it is still true."""
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=NAME)
    store.mark_rebuild_complete(NAME)
    result = store.add_aliases(entity_ref=NAME, identifiers=["brisavt"])
    assert result["grew"] and _row(conn)["rebuild_state"] == "complete"
    assert (result["carried_waiting"], result["carried_waiting_aliases"]) == (False, ["brisavt"])
    opened = [row[0] for row in conn.execute(
        "SELECT kind FROM blackhole_notifications WHERE state='open' ORDER BY rowid")]
    assert opened == ["rebuild_complete"]
    assert store.pending_rebuild_names() == set()
    assert "brisavt" in store.blackholed_name_terms() and "brisavt" not in store.blackholed_name_terms(view=OWNER)
    assert EntityBoundary(conn).mentions_protected("a note for brisavt")   # every share: at once


def test_add_aliases_refuses_an_entry_that_is_not_there(conn):
    with pytest.raises(ValueError):
        BlackholeStore(conn).add_aliases(entity_ref="Nobody Atall", aliases=["x"])


def test_a_notice_replaces_the_words_of_a_new_entrys_notification(conn):
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=NAME, notice="carried, and waiting for you")
    store.blackhole_entity(entity_ref="Perrin Ashgrove")
    messages = [row[0] for row in conn.execute("SELECT message FROM blackhole_notifications ORDER BY rowid")]
    assert messages[0] == "carried, and waiting for you"
    assert messages[1] == ("'Perrin Ashgrove' is now off-limits. A rebuild is needed before it disappears from "
                           "summaries, briefs and digests; until then those are withheld from everyone but you.")


# --- re-check R2-L5 (c): names added to an entry drop every share index, as a new entry does -------------------------

def test_adding_names_to_an_entry_drops_the_share_indexes_of_before(conn, monkeypatch):
    """An entry the owner had already made gains a contact's names and identifiers from the upgrade step. Every
    share index built before that was built without them, and must not be served until it is rebuilt. No test held
    the purge: with it removed from `add_aliases` all 995 carry and boundary tests passed (the re-check's fault
    `own2_added_names_do_not_drop_the_search_indexes`). An entry that gains nothing drops nothing."""
    from topos.features.lifecycle import blackhole

    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=NAME)
    store.mark_rebuild_complete(NAME)
    purged = []
    monkeypatch.setattr(blackhole, "_purge_message_search", lambda c: purged.append(c))
    assert store.add_aliases(entity_ref=NAME, aliases=["Quorra"], identifiers=[EMAIL])["grew"]
    assert purged == [conn]
    assert not store.add_aliases(entity_ref=NAME, aliases=["quorra"], identifiers=[EMAIL])["grew"]
    assert purged == [conn]


def test_the_purge_is_the_real_one_and_deletes_the_indexes_of_this_database(conn, tmp_path, monkeypatch):
    """What `_purge_message_search` does, so the test above is not about a name: `search_index.purge_for_database`
    is called with the store's own connection."""
    from topos.permissions_v2 import search_index

    seen = []
    monkeypatch.setattr(search_index, "purge_for_database", lambda c: seen.append(c))
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=NAME)
    assert seen == [conn]
    store.add_aliases(entity_ref=NAME, identifiers=[EMAIL])
    assert seen == [conn, conn]
