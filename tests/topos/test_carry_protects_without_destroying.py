"""Review R1 (node), R-B1: the carry step protects and destroys nothing, and the clean-up matches whole words.

protects: the upgrade step that carries the older model's explicit per-person excludes into Off-limits used to run
each new entry's clean-up of derived text by itself, at the first start on 1.5.0, and that clean-up matched every
alias as a plain substring. One excluded contact with the username "al" took out 28% of an invented home's
retrieval index and blanked 37% of the owner's own home-chat turns ("also", "normal"), unasked and unrecoverable.
The owner's decision ("protect without destroying"):
  - the step makes each entry and leaves it waiting (`pending`); no derived text is deleted, closed, blanked or
    overwritten by the step, and no upgrade step ever rewrites a home-chat session;
  - the clean-up the owner starts matches a name as whole words of the normalised text, and skips a term of fewer
    than three letters or digits; a handle, a username and an id match only as themselves, the same way;
  - a row that really names the person is still withdrawn when the owner starts the clean-up.
Every person, handle and id here is invented.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.blackhole_rebuild import _mentions, rebuild_for_blackhole
from topos.features.lifecycle.contact_excludes import carry_contact_excludes
from topos.home_chat.schema import ensure_home_chat_schema
from topos.permissions_v2.protection_clock import TOMBSTONES_SQL
from topos.storage.canonical import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

EXCLUDE = {"name_visibility": "normal", "row_visibility": "exclude_from_grants"}
DATASET = "dataset-1"
SOURCE = "messages-source"
NOW = "2026-10-01T10:00:00Z"
NAME = "Quorra Vellaby"
# Derived tables the clean-up can touch, each read whole so that "nothing changed" means every byte.
DERIVED = ("signal_embeddings", "signal_objects", "signal_dimension_briefs", "signal_facts", "user_goals",
           "community_names", "topic_clusters", "topic_cluster_members", "home_chat_sessions")
# Ordinary sentences that hold the letters of a short alias inside longer words, and name nobody.
UNRELATED = ("We also finished the normal walk by the canal.", "Totally fine, the usual plan for the weekend.",
             "Samples of the same paint arrived, and the jam is done.")
# Sentences that really name the excluded person, by the name the owner saved.
RELATED = (f"Dinner with {NAME} went late.", f"Met {NAME} about the lease.")


def _history(*turns):
    messages, previous = {}, None
    for number, text in enumerate(turns):
        turn_id = f"turn-{number}"
        messages[turn_id] = {"id": turn_id, "role": "user", "content": text, "parentId": previous, "childrenIds": [],
                             "timestamp": 1790000000 + number, "done": True}
        if previous:
            messages[previous]["childrenIds"].append(turn_id)
        previous = turn_id
    return json.dumps({"version": 3, "messages": messages, "currentId": previous})


def _derive(c, key, text):
    """One row of every kind of derived text the clean-up reads, all carrying `text`."""
    c.execute("INSERT INTO signal_embeddings (embedding_id, record_id, source_id, text_preview, search_text, "
              "vector_format, chunk_index, record_type) VALUES (?,?,?,?,?,?,?,?)",
              (f"emb-{key}", f"record-{key}", SOURCE, text, text, "none", 0, "message"))
    c.execute("INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, "
              "confidence, source_refs_json, valid_from, extractor_version, created_at, updated_at, created_by, "
              "updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (f"obj-{key}", "general", "attention_summary", f"window-{key}", json.dumps({"summary": text}), 0.9, "[]",
               NOW, "v1", NOW, NOW, "test", "test"))
    c.execute("INSERT INTO signal_dimension_briefs (brief_id, signal_dimension, head_revision_id, structured_json, "
              "markdown_body, revision_number, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?)",
              (f"brief-{key}", f"dimension-{key}", "rev-1", "{}", "- " + text, 1, NOW, "test"))
    c.execute("INSERT INTO signal_facts (fact_id, dimension, payload_json, created_at) VALUES (?,?,?,?)",
              (f"fact-{key}", "general", json.dumps({"group_key": text, "count": 3}), NOW))
    c.execute("INSERT INTO user_goals (goal_id, goal_text, payload_json, created_at) VALUES (?,?,?,?)",
              (f"goal-{key}", text, "{}", NOW))
    c.execute("INSERT INTO community_names (name_id, name, fingerprint_json, source, created_at, times_matched) "
              "VALUES (?,?,?,?,?,?)", (f"name-{key}", text, "{}", "test", NOW, 1))
    c.execute("INSERT INTO topic_clusters (cluster_id, label, dimension, member_count, source_mix_json, "
              "label_terms_json, centroid_preview, metadata_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (f"cluster-{key}", text, "general", 1, "{}", "[]", text, json.dumps({"term_label": "walks"}), NOW, NOW))
    c.execute("INSERT INTO topic_cluster_members (member_id, cluster_id, record_id, source_id, record_type, "
              "text_preview, weight, metadata_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (f"member-{key}", f"cluster-{key}", f"record-{key}", SOURCE, "message", text, 1.0, "{}", NOW))
    c.execute("INSERT INTO home_chat_sessions (id, user_id, engine_id, title, history_json) VALUES (?,?,?,?,?)",
              (f"chat-{key}", "owner-user-1", "engine-1", text[:40], _history(text, "And what came after?")))


def _snapshot(c, keys=None):
    """Every row of every derived table, as bytes; with `keys`, only the rows made for those keys."""
    out = {}
    for table in DERIVED:
        rows = [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY 1")]
        if keys is not None:
            rows = [row for row in rows if str(row[0]).rsplit("-", 1)[-1] in keys]
        out[table] = rows
    return out


@pytest.fixture()
def home(tmp_path):
    c = sqlite3.connect(str(tmp_path / "canonical.db"), check_same_thread=False)
    apply_all_migrations(c)
    c.execute(TOMBSTONES_SQL)
    manager = ConversationsTablesManager(c)
    manager.ensure_tables()
    ensure_home_chat_schema(c)
    c.execute("INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, known_usernames_json) "
              "VALUES ('contact-excluded', ?, ?, ?, ?)", (DATASET, SOURCE, NAME, json.dumps(["al"])))
    c.execute("INSERT INTO contact_identifiers (dataset_id, source_id, identifier, identifier_type, contact_id) "
              "VALUES (?, ?, 'sam', 'username', 'contact-excluded')", (DATASET, SOURCE))
    c.commit()
    manager.update_contact_sharing_policy(dataset_id=DATASET, contact_id="contact-excluded", sharing_policy=EXCLUDE)
    for number, text in enumerate(UNRELATED):
        _derive(c, f"u{number}", text)
    for number, text in enumerate(RELATED):
        _derive(c, f"r{number}", text)
    c.commit()
    yield c
    c.close()


def _entries(c):
    return {row[0]: row[1] for row in c.execute("SELECT canonical_name, rebuild_state FROM entity_blackholes")}


# ---------------------------------------------------------------------------------------------------- the step

def test_the_step_makes_the_entry_and_withdraws_nothing(home):
    """R-B1. Rule: `carry_contact_excludes` never runs a clean-up. Remove it (call `rebuild_for_blackhole` for a
    new entry again) and the rows naming the person, and here also every row holding "al" or "sam" inside a
    longer word, change."""
    before = _snapshot(home)
    out = carry_contact_excludes(home)
    home.commit()
    assert out["carried"] == 1
    assert _snapshot(home) == before                      # not one derived row deleted, closed, blanked or rewritten
    assert _entries(home) == {NAME: "pending"}            # the state that withholds, until the owner starts it
    assert out["clean_ups_waiting"] == 1 and "rebuilds_failed" not in out


def test_the_step_leaves_the_owners_home_chat_as_it_was_even_where_it_names_the_person(home):
    """R-B1: an upgrade step never rewrites `home_chat_sessions`, the owner's own conversations."""
    before = [tuple(row) for row in home.execute("SELECT * FROM home_chat_sessions ORDER BY id")]
    assert any(NAME in str(row) for row in before), "the fixture names the person in a chat turn"
    carry_contact_excludes(home)
    home.commit()
    assert [tuple(row) for row in home.execute("SELECT * FROM home_chat_sessions ORDER BY id")] == before


def test_a_rebuild_run_by_an_upgrade_step_never_rewrites_home_chat(home):
    """The runner's own re-run of every clean-up (derived_rebuild target `blackhole_rebuilds`, declared by two
    earlier releases) is an upgrade step too: it withdraws the other derived text and leaves the chats alone.
    Rule: `runner._exec_derived_rebuild` passes `home_chat=False`."""
    from topos.upgrades.runner import _exec_derived_rebuild

    BlackholeStore(home).blackhole_entity(entity_ref=NAME)
    chats = [tuple(row) for row in home.execute("SELECT * FROM home_chat_sessions ORDER BY id")]
    out = _exec_derived_rebuild({"params": {"targets": ["blackhole_rebuilds"]}}, home)
    home.commit()
    assert out["targets"]["blackhole_rebuilds"]["entities"] == 1
    assert [tuple(row) for row in home.execute("SELECT * FROM home_chat_sessions ORDER BY id")] == chats
    bodies = dict(home.execute("SELECT brief_id, markdown_body FROM signal_dimension_briefs"))
    assert bodies["brief-r0"] == "" and bodies["brief-u0"]          # the step still did its other work


# ------------------------------------------------------------------------------- the clean-up the owner starts

def test_the_clean_up_the_owner_starts_withdraws_what_names_the_person_and_nothing_else(home):
    """A real mention is still withdrawn, and the rows that only hold an alias inside a longer word are untouched
    byte for byte. Rule: `_mentions` matches a term as whole words. Make it a substring test again and the three
    unrelated rows of every table go ("also", "normal", "usual" hold "al"; "Samples", "same" hold "sam")."""
    carry_contact_excludes(home)
    home.commit()
    unrelated = _snapshot(home, keys={"u0", "u1", "u2"})
    report = rebuild_for_blackhole(home, NAME)
    home.commit()
    assert report.details["status"] == "complete" and _entries(home) == {NAME: "complete"}
    assert _snapshot(home, keys={"u0", "u1", "u2"}) == unrelated
    for key in ("r0", "r1"):
        assert home.execute("SELECT COUNT(*) FROM signal_embeddings WHERE embedding_id=?", (f"emb-{key}",)).fetchone()[0] == 0
        assert home.execute("SELECT markdown_body FROM signal_dimension_briefs WHERE brief_id=?", (f"brief-{key}",)).fetchone()[0] == ""
        assert home.execute("SELECT COUNT(*) FROM user_goals WHERE goal_id=?", (f"goal-{key}",)).fetchone()[0] == 0
        assert home.execute("SELECT valid_to FROM signal_objects WHERE object_id=?", (f"obj-{key}",)).fetchone()[0] is not None
        history = json.loads(home.execute("SELECT history_json FROM home_chat_sessions WHERE id=?", (f"chat-{key}",)).fetchone()[0])
        assert history["messages"]["turn-0"]["content"] == "" and history["messages"]["turn-1"]["content"]
    assert report.embeddings_withdrawn == 2 and report.briefs_invalidated == 2 and report.goals_withdrawn == 2


@pytest.mark.parametrize("text, terms, expected", [
    # a name is matched as whole words
    ("Lunch with Sam on Friday.", {"sam"}, True),
    ("Sam's bike is in the hall.", {"sam"}, True),                     # a possessive is the word
    ("Samples of the same paint.", {"sam"}, False),                    # inside a longer word
    ("We also finished the normal walk.", {"al"}, False),              # under three characters: skipped
    ("Al came by.", {"al"}, False),                                    # ... even where it stands alone
    ("J rang about the keys.", {"j"}, False),
    ("Dinner with Quorra Vellaby went late.", {"quorra vellaby"}, True),
    ("quorra  \n vellaby, again", {"quorra vellaby"}, True),           # whatever separates the words
    ("Quorravellaby is one word here.", {"quorra vellaby"}, False),
    ("Ask Will about it.", {"will"}, True),
    ("Goodwill and willing helpers.", {"will"}, False),
    # a handle, a username, an id: only as themselves
    ("Write to q.vellaby@fernmail.example today.", {"q.vellaby@fernmail.example"}, True),
    ("The mail was late; an example follows.", {"q.vellaby@fernmail.example"}, False),
    ("The network was down and homework is done.", {"work"}, False),
    ("Back to work on Monday.", {"work"}, True),
    ("Call +1 555 0142 0137 after six.", {"+1 555 0142 0137"}, True),
    ("Lost a contact lens; dark mode is the default.", {"3f9c2e71-6b0d default contact 7471fce8530d7bd0"}, False),
    # the stored forms the clean-up already read: JSON escapes and HTML entities
    (json.dumps({"summary": "Old Harbor- Rey’s Place at noon"}), {"old harbor- rey place"}, True),
    ("Rey&#39;s Place at noon", {"rey place"}, True),
    ("", {"sam"}, False),
    (None, {"sam"}, False),
])
def test_a_term_matches_whole_words_and_never_under_three_characters(text, terms, expected):
    assert _mentions(text, terms) is expected
