"""IF-5: a journal entry as a grant's evidence — loaded, proven, checked and assessed like a message, never more.

protects: before this family, the evidence layer knew two leaf tables and refused every other one, so the
owner's journal (on the node this was measured on: 116 of 192 current facts and 1,928 goals cite it) could
never be shared. Opening a table is exactly where a boundary leaks, so these tests pin what the family must
still refuse:
  - with its flag off the family does not exist: a journal identity does not load;
  - a row is evidence only when its door or the owner proved it the owner's (capture_receipts);
  - every message check still runs: role, content bounds, the NSFW hard withhold, owner-only, exclusions,
    Off-limits over every column (people, places), and copies — with same-source twins as one record;
  - the machine assessment sees no neighbours, never labels a journal entry `none`, and raises any
    special-category cue to `special`; its revision is the journal's own, so a journal floor change stales
    journal assessments only;
  - message behaviour is unchanged while the family is off.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any

import pytest

from topos.permissions_v2 import capture_receipts
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceBinding, EvidenceResolver, EvidenceReviewStore
from topos.permissions_v2.evidence_families import (JOURNAL_FLAG, enabled_family, enabled_tables, family, rank_time_us,
                                                    released, within)
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
from topos.storage.db.migrations import apply_all_migrations

OWNER = "owner-1"
RESOURCE = "resource-1"
SOURCE = "time_log"
DATASET = f"{OWNER}:topos:default"
APP = "owner-journal-app"
WORDS = "Finished the draft today and walked home the long way."
DAY_US = 86_400 * 1_000_000


@contextmanager
def owner():
    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user=OWNER))
    try:
        yield
    finally:
        reset_principal(token)


@pytest.fixture()
def node(tmp_path, monkeypatch):
    monkeypatch.setenv(JOURNAL_FLAG, "true")
    from topos.storage.canonical.ai_chat import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import (ensure_conversation_messages_table,
                                                              ensure_conversations_table)
    path = tmp_path / "canonical.db"
    conn = sqlite3.connect(str(path))
    # The message tables are created by their first writer on a real node; the copy rule needs both.
    CanonicalTablesManager(conn)
    ensure_conversations_table(conn)
    ensure_conversation_messages_table(conn)
    apply_all_migrations(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('user_id', ?)", (OWNER,))
    conn.execute("""CREATE TABLE IF NOT EXISTS source_runtime_installs (install_id TEXT PRIMARY KEY, scope_key TEXT,
        source_id TEXT, version_id TEXT, status TEXT, is_active INTEGER, source_definition_json TEXT,
        source_version_row_json TEXT, failure_reason TEXT, created_at TEXT, updated_at TEXT)""")
    for number, source in enumerate((SOURCE, "other_journal")):
        conn.execute("INSERT INTO source_runtime_installs (install_id, scope_key, source_id, version_id, status, "
                     "is_active, source_definition_json) VALUES (?,?,?,?,?,?,?)",
                     (f"install-{number}", json.dumps({"user_id": OWNER, "topos_id": RESOURCE, "device_id": "*",
                                                       "dataset_id": DATASET if source == SOURCE else f"{OWNER}:x:y"}),
                      source, "v1", "active", 1, json.dumps({"source_id": source})))
    conn.commit()
    conn.close()
    ensure_protection_clock(path, owner_id=OWNER)
    return path


def _db(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _entry(path, entry_id: str, content: str = WORDS, *, source: str = SOURCE, entry_at="2026-09-10T08:30:00",
           writer_class="owner_import", app=None, dataset=DATASET, **extra: Any) -> None:
    columns = {"entry_id": entry_id, "entry_at": entry_at, "content": content, "source_id": source,
               "writer_class": writer_class, "writer_app_id": app, "writer_dataset_id": dataset, **extra}
    with _db(path) as conn:
        conn.execute(f"INSERT OR REPLACE INTO journal_entries ({','.join(columns)}) VALUES "
                     f"({','.join('?' * len(columns))})", list(columns.values()))


def _resolver(path):
    return EvidenceResolver(path, binding=EvidenceBinding(environment_id="permissions-beta-test", node_id="node-1",
                                                          resource_id=RESOURCE, owner_id=OWNER))


def _identity(resolver, entry_id: str, source: str = SOURCE):
    return resolver._identity("journal_entries", entry_id, source)


def _check(path, entry_id: str, source: str = SOURCE):
    """The whole-record source checks for one journal row: None when it passes, else the refusal code."""
    from topos.permissions_v2.message_evidence import _source_checks
    resolver = _resolver(path)
    with resolver._read() as (conn, _floor):
        identity = _identity(resolver, entry_id, source)
        try:
            _source_checks(resolver, conn, identity, resolver._load(conn, identity))
        except PolicyError as exc:
            return exc.code
    return None


# --- the registry ----------------------------------------------------------------------------------

def test_the_journal_family_is_declared_once(monkeypatch):
    journal = family("journal_entries")
    assert (journal.name, journal.id_column, journal.time_column, journal.time_semantics, journal.dataset_kind,
            journal.kind) == ("journal_entry", "entry_id", "entry_at", "stated_day_v1", "node_resource", "journal_entry")
    monkeypatch.delenv(JOURNAL_FLAG, raising=False)
    assert enabled_tables() == ("conversation_messages", "ai_chat_messages")
    with pytest.raises(PolicyError) as disabled:
        enabled_family("journal_entries")
    assert disabled.value.code == "evidence_family_disabled"
    monkeypatch.setenv(JOURNAL_FLAG, "true")
    assert enabled_tables() == ("conversation_messages", "ai_chat_messages", "journal_entries")


@pytest.mark.parametrize("table", ["activity_events", "browser_visits", "location_events", "journal_entries;", None])
def test_no_other_table_is_a_family(table):
    with pytest.raises(PolicyError) as refused:
        family(table)
    assert refused.value.code == "unsupported_evidence_table"


def test_a_journal_rows_time_is_its_stated_day():
    row = {"entry_at": "2026-09-10T08:30:00"}
    day = 1788998400 * 1_000_000   # 2026-09-10T00:00:00Z
    assert rank_time_us("journal_entries", row) == day
    assert released("journal_entries", row, "day") == day // 1_000_000
    assert released("journal_entries", row, "second") is None
    assert within("journal_entries", row, day - 14 * 3600 * 10**6, day + 36 * 3600 * 10**6 - 1)
    assert not within("journal_entries", row, day, day + 36 * 3600 * 10**6)


# --- identity and load -----------------------------------------------------------------------------

def test_a_journal_identity_is_scoped_by_its_source_and_carries_no_dataset():
    from topos.permissions_v2.evidence import EvidenceIdentity
    binding = EvidenceBinding(environment_id="e", node_id="n", resource_id=RESOURCE, owner_id=OWNER).model_dump()
    ok = EvidenceIdentity.parse(dict(binding=binding, table="journal_entries", record_id="e1", source_id=SOURCE,
                                     dataset_kind="node_resource", dataset_id=None))
    assert ok.table == "journal_entries"
    for bad in (dict(source_id=None), dict(dataset_kind="row_dataset", dataset_id=DATASET)):
        with pytest.raises(Exception):
            EvidenceIdentity.parse(dict(binding=binding, table="journal_entries", record_id="e1", source_id=SOURCE,
                                        dataset_kind="node_resource", dataset_id=None, **bad))


def test_with_the_flag_off_a_journal_identity_does_not_load(node, monkeypatch):
    _entry(node, "e1")
    monkeypatch.delenv(JOURNAL_FLAG, raising=False)
    resolver = _resolver(node)
    with resolver._read() as (conn, _floor), pytest.raises(PolicyError) as refused:
        resolver._load(conn, _identity(resolver, "e1"))
    assert refused.value.code == "evidence_family_disabled"


def test_a_journal_row_loads_by_its_source_with_its_posture_revision(node):
    _entry(node, "e1")
    resolver = _resolver(node)
    with resolver._read() as (conn, _floor):
        row = resolver._load(conn, _identity(resolver, "e1"))
        assert row["content"] == WORDS and "_p2b_source_revision" in row
        with pytest.raises(PolicyError) as missing:
            resolver._load(conn, _identity(resolver, "e1", source="other_journal"))
        assert missing.value.code == "evidence_missing"


# --- the owner proof and the message checks ----------------------------------------------------------

def test_a_door_proven_row_passes_every_source_check(node):
    _entry(node, "e1")
    assert _check(node, "e1") is None


@pytest.mark.parametrize("writer, app, dataset", [
    ("cp_relay", None, DATASET),                   # a grantee or an unlisted app, through the relay
    ("third_party", None, DATASET),
    ("owner_app", "some-other-app", DATASET),      # an owner write from an app the owner never attested
    ("owner_import", None, f"{OWNER}:x:y"),        # into a dataset the source's install is not scoped to
    (None, None, None),                            # pre-stamp, never attested
])
def test_a_row_the_owner_never_proved_is_refused(node, writer, app, dataset):
    _entry(node, "e1", writer_class=writer, app=app, dataset=dataset)
    assert _check(node, "e1") == "journal_owner_unproven"


def test_the_owners_receipt_proves_a_pre_stamp_row_at_its_current_words(node):
    _entry(node, "e1", writer_class=None, dataset=None)
    with _db(node) as conn:
        preview = capture_receipts.preview(conn, owner_id=OWNER, table="journal_entries", source_id=SOURCE, app_id=APP)
        capture_receipts.attest(conn, owner_id=OWNER, table="journal_entries", source_id=SOURCE, app_id=APP,
                                preview_digest=preview["preview_digest"], confirm=True)
    assert _check(node, "e1") is None
    with _db(node) as conn:
        conn.execute("UPDATE journal_entries SET content='Different words now.' WHERE entry_id='e1'")
    assert _check(node, "e1") == "journal_owner_unproven"


def test_an_ambient_posture_caps_a_journal_row(node):
    _entry(node, "e1")
    with _db(node) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS user_ingestion_sources (dataset_id TEXT, source_id TEXT, enabled INTEGER, "
                     "posture TEXT)")
        conn.execute("INSERT INTO user_ingestion_sources (dataset_id, source_id, enabled, posture) VALUES (?,?,1,'ambient')",
                     (DATASET, SOURCE))
    assert _check(node, "e1") == "not_owner_authored"


@pytest.mark.parametrize("mutation, code", [
    (dict(content_nsfw=1), "unsupported_message_content"),
    (dict(content="   "), "unsupported_message_content"),
    (dict(content="x" * 100_001), "unsupported_message_content"),
])
def test_the_message_content_checks_still_run(node, mutation, code):
    _entry(node, "e1", **mutation)
    assert _check(node, "e1") == code


def test_same_source_twins_are_one_record_and_the_earliest_is_it(node):
    _entry(node, "e2", entry_at="2026-09-10T08:30:00")
    _entry(node, "e1", entry_at="2026-09-10T08:30:00")    # same time: the smaller id is the member
    _entry(node, "e0", entry_at="2026-09-11T08:30:00")    # a later re-push of the same words
    assert _check(node, "e1") is None
    assert _check(node, "e2") == "journal_copy_alias"
    assert _check(node, "e0") == "journal_copy_alias"


def test_a_copy_in_another_source_or_a_message_withholds_every_copy(node):
    _entry(node, "e1")
    _entry(node, "x1", source="other_journal", dataset=f"{OWNER}:x:y")
    assert _check(node, "e1") == "independent_copy_lineage"


def test_a_message_twin_withholds_the_journal_row(node):
    _entry(node, "e1")
    with _db(node) as conn:
        conn.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, "
                     "source_id) VALUES ('m1','c1','user','2026-09-10T08:30:00Z',?,'chatgpt_ui_conversation')", (WORDS,))
    assert _check(node, "e1") == "independent_copy_lineage"


def test_with_the_flag_off_a_journal_twin_changes_nothing_for_a_message(node, monkeypatch):
    from topos.permissions_v2.evidence import EvidenceResolver as Resolver
    _entry(node, "e1")
    resolver = _resolver(node)
    message = resolver._identity("ai_chat_messages", "m1", "chatgpt_ui_conversation")
    with resolver._read() as (conn, _floor):
        assert Resolver._known_copies(conn, message, {"content": WORDS}) is True     # flag on: a copy
        monkeypatch.delenv(JOURNAL_FLAG, raising=False)
        assert Resolver._known_copies(conn, message, {"content": WORDS}) is False    # flag off: as before


# --- floors: owner-only, exclusions, Off-limits over every column ------------------------------------

def _floors_code(path, entry_id: str):
    from topos.permissions_v2.message_evidence import _floors, snapshot_message
    resolver = _resolver(path)
    with resolver._read() as (conn, floor):
        identity = _identity(resolver, entry_id)
        try:
            snapshot, rows = snapshot_message(resolver, conn, floor, identity)
            _floors(resolver, conn, snapshot, rows, frozenset())
        except PolicyError as exc:
            return exc.code
    return None


def test_owner_only_and_exclusions_withhold_a_journal_row(node):
    _entry(node, "e1")
    _entry(node, "e2", "Another entry, about something else.")
    assert _floors_code(node, "e1") is None
    with _db(node) as conn:
        conn.execute("INSERT INTO owner_only_records (canonical_table, record_id, created_at, updated_at) "
                     "VALUES ('journal_entries','e1','t','t')")
        conn.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key, created_at) "
                     "VALUES ('x1','record','e2','t')")
    assert _floors_code(node, "e1") == "owner_only"
    assert _floors_code(node, "e2") == "intelligence_excluded"


def test_an_off_limits_name_in_any_column_withholds_the_row(node):
    from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
    with _db(node) as conn:
        apply_entity_blackhole_v1_up(conn)
        conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name, "
                     "aliases_json, created_at) VALUES ('b1','','quillon marsh','Quillon Marsh','[]','t')")
    _entry(node, "e-text", "Lunch with Quillon Marsh near the harbour.")
    _entry(node, "e-people", "Lunch near the harbour.", people="Quillon Marsh")
    _entry(node, "e-place", "Coffee after the run.", place_name="Quillon Marsh Cafe")
    _entry(node, "e-clear", "Coffee after the run, alone.")
    for entry_id in ("e-text", "e-people", "e-place"):
        assert _floors_code(node, entry_id) == "entity_protected", entry_id
    assert _floors_code(node, "e-clear") is None


def _off_limits(path, canonical: str, aliases=()):
    """One synthetic Off-limits person with no entity row: the boundary's terms are the names alone."""
    from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
    with _db(path) as conn:
        apply_entity_blackhole_v1_up(conn)
        conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name, "
                     "aliases_json, created_at) VALUES ('b1','',?,?,?,'t')",
                     (canonical.lower(), canonical, json.dumps(list(aliases))))


@pytest.mark.parametrize("content", [
    "Lunch with Quillon near the harbour.",          # a bare first name
    "Marsh came by after the run.",                  # a bare last name
    "Borrowed Quillon's bike.",                      # a possessive of a part
    "Borrowed Quillon\u2019s bike.",                 # a curly possessive
    "Lunch with QUILLON near the harbour.",          # case
    "Lunch with Qu\u00edllon near the harbour.",     # an accent
    "Lunch with Quil\u200blon near the harbour.",    # an invisible character inside the part
    "Called Vessarine today.",                       # a bare part of an alias
    "Called Ellowick today.",                        # a bare part of a hyphenated alias
])
def test_a_bare_part_of_an_off_limits_name_withholds_a_journal_row(node, content):
    """OD-58 held-out (30 Sep): 57 of 57 whole-name, alias, possessive, invisible-character, punctuated and
    column-only references withheld; the one bare-first-name and the one bare-last-name row released, because
    a whole-name term is one skeleton. For journal rows the boundary now reads each part of the names too."""
    _off_limits(node, "Quillon Marsh", aliases=["Vessarine Q. Marsh-Ellowick"])
    _entry(node, "e1", content)
    assert _floors_code(node, "e1") == "entity_protected"


def test_a_bare_name_part_in_any_column_withholds_a_journal_row(node):
    """The journal Off-limits surface is every column of the row (IF-5 section 1), for parts as for names."""
    _off_limits(node, "Quillon Marsh")
    _entry(node, "e-people", "Lunch near the harbour.", people="Quillon")
    _entry(node, "e-place", "Coffee after the run.", place_name="Marsh Cafe")
    _entry(node, "e-meta", "Tea after the run.", metadata_json=json.dumps({"tags": ["quillon"]}))  # distinct text: twins alias
    for entry_id in ("e-people", "e-place", "e-meta"):
        assert _floors_code(node, entry_id) == "entity_protected", entry_id


@pytest.mark.parametrize("content", [
    "Walked the marshland at dusk.",                 # a part inside a longer word
    "Marshmallows by the fire, then home.",
    "Quillonesque weather, if that is a word.",
    "A quiet evening, nothing else.",
])
def test_a_name_part_inside_a_longer_word_does_not_withhold_a_journal_row(node, content):
    """A part is a whole word: the rule adds withholds for the name, not for every word containing it."""
    _off_limits(node, "Quillon Marsh")
    _entry(node, "e1", content)
    assert _floors_code(node, "e1") is None


def test_a_two_letter_name_part_is_not_a_part(node):
    """An initial or a two-letter particle would match half the language; only the whole-term scan reads them."""
    _off_limits(node, "Bo Quillon", aliases=["B. Q. Marsh"])
    _entry(node, "e-bo", "Bo came by after the run.")
    _entry(node, "e-q", "Q came by after the run.")
    _entry(node, "e-part", "Quillon came by after the run.")
    _entry(node, "e-alias-part", "Marsh came by after the run.")
    assert _floors_code(node, "e-bo") is None
    assert _floors_code(node, "e-q") is None
    assert _floors_code(node, "e-part") == "entity_protected"
    assert _floors_code(node, "e-alias-part") == "entity_protected"


def test_the_census_can_count_the_rows_only_a_name_part_withholds(node):
    """`entity_protected` stays one code; the boundary says which rows the name-part rule alone accounts for."""
    from topos.permissions_v2.entity_boundary import EntityBoundary
    _off_limits(node, "Quillon Marsh")
    _entry(node, "e-part", "Lunch with Quillon near the harbour.")
    _entry(node, "e-whole", "Lunch with Quillon Marsh near the harbour.")
    _entry(node, "e-clear", "Lunch near the harbour.")
    with _db(node) as conn:
        boundary = EntityBoundary(conn)
        rows = {row["entry_id"]: dict(row) for row in conn.execute("SELECT * FROM journal_entries")}
    assert [boundary.name_part_match_only("journal_entries", rows[i]) for i in ("e-part", "e-whole", "e-clear")] == [
        True, False, False]
    assert not boundary.name_part_match_only("conversation_messages", rows["e-part"])
    for entry_id, code in (("e-part", "entity_protected"), ("e-whole", "entity_protected"), ("e-clear", None)):
        assert _floors_code(node, entry_id) == code, entry_id


# --- the assessment -----------------------------------------------------------------------------------

def _prepare(path, entry_id: str):
    from topos.permissions_v2.automatic_message_review import prepare
    resolver = _resolver(path)
    with owner():
        reviews = EvidenceReviewStore(path.parent / "reviews.db", resolver=resolver)
        return resolver, reviews, prepare(resolver, reviews, _identity(resolver, entry_id))


def _labels(prepared, **values):
    from topos.permissions_v2.automatic_message_review import parse_assessment
    base = dict(domains=["hobbies"], sensitivity="none", speech="original_message", protected_content="none")
    return parse_assessment({**base, **values}, prepared["snapshot"].message)


def test_a_journal_entry_is_prepared_without_neighbours(node):
    _entry(node, "e1")
    _entry(node, "e2", "The entry written after it.", entry_at="2026-09-10T09:30:00")
    _resolver_, _reviews, prepared = _prepare(node, "e1")
    assert prepared["input"]["before"] == [] and prepared["input"]["after"] == []
    assert prepared["input"]["target"] == WORDS


def test_a_journal_entry_is_never_labelled_none_and_a_special_cue_makes_it_special(node):
    from topos.permissions_v2.automatic_message_review import apply_family_floors
    _entry(node, "e1")
    _resolver_, _reviews, prepared = _prepare(node, "e1")
    raised = apply_family_floors("journal_entries", _labels(prepared), prepared["input"])
    assert raised.sensitivity == "personal"
    special = apply_family_floors("journal_entries", _labels(prepared),
                                  {**prepared["input"], "target": "Saw my therapist about the anxiety again."})
    assert special.sensitivity == "special"
    unknown = apply_family_floors("journal_entries", _labels(prepared, sensitivity="unknown"), prepared["input"])
    assert unknown.sensitivity == "unknown"   # a floor never turns "cannot tell" into an answer
    # A message keeps the message floors only.
    assert apply_family_floors("conversation_messages", _labels(prepared), prepared["input"]).sensitivity == "none"


def test_the_boundary_decides_a_journal_entrys_protected_content(node):
    """OD-58: the model's `unknown` defers to the Off-limits boundary for journal entries only."""
    from topos.permissions_v2.automatic_message_review import JOURNAL_FLOORS_VERSION, apply_family_floors
    assert JOURNAL_FLOORS_VERSION == "journal-entry-floors/v2"
    _entry(node, "e1")
    _resolver_, _reviews, prepared = _prepare(node, "e1")
    journal = apply_family_floors("journal_entries", _labels(prepared, protected_content="unknown"), prepared["input"])
    assert journal.protected_content == "none"
    present = apply_family_floors("journal_entries", _labels(prepared, protected_content="present"), prepared["input"])
    assert present.protected_content == "present"        # the model's own finding stays binding
    message = apply_family_floors("conversation_messages", _labels(prepared, protected_content="unknown"),
                                  prepared["input"])
    assert message.protected_content == "unknown"        # messages are untouched


def test_a_published_journal_assessment_is_the_journals_own_revision(node, monkeypatch):
    from topos.permissions_v2 import automatic_message_review as amr
    from topos.permissions_v2.message_evidence import qualify_automatic_message
    _entry(node, "e1")
    resolver, reviews, prepared = _prepare(node, "e1")
    with owner():
        review = amr.publish(resolver, reviews, prepared, _labels(prepared), now=1)
    assert review.rubric_revision == amr.rubric_revision_for("journal_entries") != amr.rubric_revision()
    assert review.classifications[0].sensitivity == "personal"
    assert amr.is_current(review, prepared)
    with resolver._read() as (conn, floor), reviews._db() as db:
        qualified, _rows = qualify_automatic_message(resolver, conn, floor, _identity(resolver, "e1"), reviews, db)
    assert qualified.classifications[0].sensitivity == "personal"
    monkeypatch.setattr(amr, "JOURNAL_FLOORS_VERSION", "journal-entry-floors/v3")
    assert not amr.is_current(review, prepared)          # a journal floor change stales journal assessments ...
    assert amr.rubric_revision_for("conversation_messages") == amr.rubric_revision()   # ... and nothing else


def test_an_unproven_row_cannot_be_prepared_for_assessment(node):
    _entry(node, "e1", writer_class="cp_relay")
    with pytest.raises(PolicyError) as refused:
        _prepare(node, "e1")
    assert refused.value.code == "journal_owner_unproven"


# --- the worker and the index -------------------------------------------------------------------------

def test_the_worker_pages_journal_entries_only_while_the_family_exists(node, monkeypatch):
    from types import SimpleNamespace
    from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
    _entry(node, "e1", entry_at="2026-09-10T08:30:00")
    _entry(node, "e2", "An older entry.", entry_at="2026-06-01T08:30:00")
    worker = AutomaticReviewWorker(_resolver(node), reviews=None)
    request = SimpleNamespace(after=1788998400 - 7 * 86400, before=1788998400 + 86400)
    assert worker._page("journal_entries", "", request) == [("e1", SOURCE, None)]
    monkeypatch.delenv(JOURNAL_FLAG, raising=False)
    assert "journal_entries" not in enabled_tables()


def test_an_index_reads_a_journal_members_live_row_and_basis(node, monkeypatch):
    from topos.permissions_v2.search_index import _family_rubric_basis, _live_rows, _member_fingerprint
    _entry(node, "e1")
    with _db(node) as conn:
        rows, facts = _live_rows(conn, {"table": "journal_entries", "record_id": "e1", "source_id": SOURCE, "facts": []})
        assert len(rows) == 1 and _member_fingerprint(rows, facts, table="journal_entries") is not None
        assert set(_family_rubric_basis()) == {"automatic_rubric_revisions"}
        monkeypatch.delenv(JOURNAL_FLAG, raising=False)
        assert _live_rows(conn, {"table": "journal_entries", "record_id": "e1", "source_id": SOURCE, "facts": []}) == ([], [])
        assert _family_rubric_basis() == {}   # a messages-only index's basis is byte for byte what it was


# --- the owner reads journal members first (OD-53 item 6) ---------------------------------------------

def _publish(path, entry_id: str, **labels):
    from topos.permissions_v2 import automatic_message_review as amr
    resolver, reviews, prepared = _prepare(path, entry_id)
    with owner():
        amr.publish(resolver, reviews, prepared, _labels(prepared, **labels), now=1)
    return resolver, reviews


def _queue(path, *, after=1788998400 - 7 * 86400, before=1788998400 + 3 * 86400, limit=10, as_owner=True):
    from types import SimpleNamespace
    from topos.permissions_v2.message_evidence import queue_journal_entries
    resolver = _resolver(path)
    with owner():
        reviews = EvidenceReviewStore(path.parent / "reviews.db", resolver=resolver)
    request = SimpleNamespace(after=after, before=before, limit=limit)
    if not as_owner:
        return queue_journal_entries(resolver, reviews, request, now=before)
    with owner():
        return queue_journal_entries(resolver, reviews, request, now=before)


def _listed(page):
    return [record.snapshot.message.identity.record_id for record in page.records]


def test_the_owner_reads_the_riskiest_would_be_released_entries_first(node):
    _entry(node, "e-work", "Shipped the release notes.", entry_at="2026-09-09T10:00:00")
    _entry(node, "e-home", "Talked through the lease renewal.", entry_at="2026-09-08T10:00:00")
    _entry(node, "e-new", "Not assessed yet.", entry_at="2026-09-10T10:00:00")
    _entry(node, "e-held", "Withheld by its labels.", entry_at="2026-09-10T11:00:00")
    _publish(node, "e-work", domains=["work"])
    _publish(node, "e-home", domains=["home"])
    # `present` is the model's own finding and stays binding; `unknown` would now defer to the boundary (OD-58).
    _publish(node, "e-held", domains=["work"], protected_content="present")
    assert _listed(_queue(node)) == ["e-home", "e-work", "e-new", "e-held"]


def test_entries_that_can_never_release_are_not_listed(node):
    _entry(node, "e-ok", entry_at="2026-09-09T10:00:00")
    _entry(node, "e-unproven", "Written through the relay.", writer_class="cp_relay", entry_at="2026-09-09T11:00:00")
    _entry(node, "e-owner-only", "Kept to myself.", entry_at="2026-09-09T12:00:00")
    _entry(node, "e-old", "From long ago.", entry_at="2026-06-01T10:00:00")
    with _db(node) as conn:
        conn.execute("INSERT INTO owner_only_records (canonical_table, record_id, created_at, updated_at) "
                     "VALUES ('journal_entries','e-owner-only','t','t')")
    assert _listed(_queue(node)) == ["e-ok"]


def test_only_the_owner_reads_the_queue_and_only_while_the_family_exists(node, monkeypatch):
    _entry(node, "e1")
    with pytest.raises(PolicyError) as refused:
        _queue(node, as_owner=False)
    assert refused.value.code == "owner_authority_required"
    monkeypatch.delenv(JOURNAL_FLAG, raising=False)
    with pytest.raises(PolicyError) as disabled:
        _queue(node)
    assert disabled.value.code == "evidence_family_disabled"


def test_the_queue_is_bounded_like_the_message_queue(node):
    for day in range(5):
        _entry(node, f"e{day}", f"Entry number {day}.", entry_at=f"2026-09-0{day + 1}T10:00:00")
    page = _queue(node, after=1788998400 - 12 * 86400, limit=2)
    assert len(page.records) == 2 and page.truncated
    with pytest.raises(PolicyError):
        _queue(node, after=1788998400 - 40 * 86400)   # a window over 31 days is refused


# --- end to end: a signed knowledge grant that lists the journal ------------------------------------------

DOMAINS = ["family", "finance", "health", "hobbies", "home", "plans", "relationships", "work"]
# The journal entry's stated day ends everywhere at 12:00Z the next day; search a minute after that.
AFTER_ITS_DAY = 1788998400 + 36 * 3600 + 60


def _journal_policy(*, kinds=("message", "fact", "goal", "relationship", "journal_entry"), precision="none"):
    """Clark's shape of rule (every domain, sensitivity none or personal) over the journal source."""
    import copy
    from tests.permissions_v2 import message_search_corpus as mc
    from tests.permissions_v2.test_knowledge_search import knowledge_policy
    raw = knowledge_policy()
    tables = ["conversation_messages", "journal_entries"]
    permit = copy.deepcopy(next(rule for rule in raw["rules"] if rule["effect"] == "permit"))
    predicate = {"kind": "all_of", "terms": [mc._atom("domain", DOMAINS), mc._atom("sensitivity", ["none", "personal"])]}
    permit["evidence_use"]["sources"] = {"kind": "only", "values": [SOURCE]}
    permit["evidence_use"]["predicate"] = copy.deepcopy(predicate)
    permit["release"]["predicate"] = copy.deepcopy(predicate)
    for form in permit["release"]["forms"]:
        form["tables"] = tables
    raw["rules"] = [permit]
    raw["source_universe"]["source_ids"] = [*raw["source_universe"]["source_ids"], SOURCE]
    raw["search"].update(tables=tables, result_types=list(kinds), release_event_time=precision)
    return raw


def _journal_node(path, tmp_path, monkeypatch, *, now=AFTER_ITS_DAY, **policy):
    from types import SimpleNamespace
    from tests.permissions_v2 import message_search_corpus as mc
    from tests.permissions_v2.message_search_harness import Node
    resolver, reviews = _publish(path, "e1", domains=["hobbies"])
    monkeypatch.setattr(mc, "NOW", now)
    node = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), tmp_path / "search-node",
                model=None, search_raw=_journal_policy(**policy), now=now)
    with owner():
        state = node.index.rebuild("grant-search", now=now)
    return node, state


def test_the_census_copy_check_expects_the_journal_basis_the_node_writes(node, tmp_path, monkeypatch):
    """With the journal flag on, the node writes the journal family's rubric revision into a knowledge grant's index
    basis. The census copy check (scripts/permissions_v2/census_copy.py) must expect it, or every copy of a node
    running with the flag voids as basis_mismatch -- which is what happened on the owner's node once candidate 9
    ran with the flag on (30 Sep 2026)."""
    import importlib
    import sys
    from pathlib import Path
    from topos.permissions_v2.search_index import index_path, root_for
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    census_copy = importlib.import_module("census_copy")
    _entry(node, "e1")
    search, state = _journal_node(node, tmp_path, monkeypatch)
    assert state["state"] == "ready"
    with sqlite3.connect(index_path(root_for(search.index.resolver.path), "grant-search")) as raw:
        basis = json.loads(raw.execute("SELECT basis_json FROM meta").fetchone()[0])
    extras = census_copy.knowledge_basis_extras()
    assert "automatic_rubric_revisions" in extras
    assert {key: value for key, value in basis.items() if key.startswith("automatic_")} == extras


def test_a_grant_that_signs_journal_entries_releases_one(node, tmp_path, monkeypatch):
    _entry(node, "e1")
    search, state = _journal_node(node, tmp_path, monkeypatch)
    assert state == {"state": "ready", "member_count": 1}
    output, refused = search.search_request("draft walked home", k=10)
    assert refused is None
    (record,) = output["records"]
    assert (record["kind"], record["content"], record["source_ids"]) == ("journal_entry", WORDS, [SOURCE])
    assert record["citations"] == [dict(record_id=record["record_id"], source_id=SOURCE, content=WORDS)]
    assert record["event_at"] is None   # the grant releases no time


@pytest.mark.parametrize("precision, expected", [("day", 1788998400), ("second", None)])
def test_a_journal_entry_releases_its_stated_day_at_most(node, tmp_path, monkeypatch, precision, expected):
    _entry(node, "e1")
    search, _state = _journal_node(node, tmp_path, monkeypatch, precision=precision)
    output, _refused = search.search_request("draft walked home", k=10)
    assert [record["event_at"] for record in output["records"]] == [expected]


def test_without_the_journal_option_nothing_from_the_journal_releases(node, tmp_path, monkeypatch):
    _entry(node, "e1")
    search, state = _journal_node(node, tmp_path, monkeypatch, kinds=("message", "fact", "goal", "relationship"))
    assert state["member_count"] == 0
    output, _refused = search.search_request("draft walked home", k=10)
    assert output["records"] == []


def test_an_entry_whose_day_has_not_ended_everywhere_waits(node, tmp_path, monkeypatch):
    _entry(node, "e1")
    # 11:59Z the next day: somewhere on Earth it is still the entry's day, so it is not yet inside the window.
    search, state = _journal_node(node, tmp_path, monkeypatch, now=AFTER_ITS_DAY - 120)
    assert state["member_count"] == 0
    output, _refused = search.search_request("draft walked home", k=10)
    assert output["records"] == []


def test_a_journal_entry_that_became_unreleasable_after_indexing_is_withheld(node, tmp_path, monkeypatch):
    _entry(node, "e1")
    search, _state = _journal_node(node, tmp_path, monkeypatch)
    with _db(node) as conn:   # the owner withdraws it: Off-limits-style record restriction
        conn.execute("INSERT INTO owner_only_records (canonical_table, record_id, created_at, updated_at) "
                     "VALUES ('journal_entries','e1','t','t')")
    output, refused = search.search_request("draft walked home", k=10)
    assert refused is not None or output["records"] == []


def test_with_the_flag_off_a_journal_grant_releases_nothing(node, tmp_path, monkeypatch):
    _entry(node, "e1")
    search, _state = _journal_node(node, tmp_path, monkeypatch)
    monkeypatch.delenv(JOURNAL_FLAG, raising=False)
    output, refused = search.search_request("draft walked home", k=10)
    assert refused is not None or output["records"] == []



def test_the_release_step_holds_a_journal_member_to_the_grant_on_its_own(node, tmp_path, monkeypatch):
    """Defence in depth: the index build already drops what the release step refuses, so the end-to-end tests
    cannot tell the two apart. `_journal_member` must refuse on its own: the option, the window, NSFW."""
    from topos.permissions_v2.search_release import MessageSearchRelease
    from topos.permissions_v2.message_evidence import qualify_automatic_message
    from topos.permissions_v2.release import source_message_decision
    from topos.permissions_v2.registry import parse_policy
    _entry(node, "e1")
    _entry(node, "e-nsfw", "Something flagged.", content_nsfw=1)
    resolver, reviews = _publish(node, "e1", domains=["hobbies"])
    day_start, day_end = 1788998400 * 10**6 - 14 * 3600 * 10**6, 1788998400 * 10**6 + 36 * 3600 * 10**6
    with resolver._read() as (conn, floor), reviews._db() as db:
        identity = _identity(resolver, "e1")
        qualified, rows = qualify_automatic_message(resolver, conn, floor, identity, reviews, db)
        row = dict(rows[next(iter(rows))])

    def member(policy, row=row, lower=day_start, upper=day_end, precision="day"):
        decision = source_message_decision(policy, qualified)
        return MessageSearchRelease._journal_member(row, identity, "r." + "c" * 64, qualified, decision, policy,
                                                    True, lower, upper, precision)
    with_option = parse_policy(_journal_policy())
    record, binding, _revision = member(with_option)
    assert record["kind"] == "journal_entry" and binding["evidence_tables"] == ["journal_entries"]
    assert record["event_at"] == 1788998400
    assert member(parse_policy(_journal_policy(kinds=("message", "fact", "goal", "relationship")))) is None
    assert member(with_option, upper=day_end - 1) is not None  # the span is half-open: its last instant is day_end - 1
    assert member(with_option, upper=day_end - 2) is None      # the day has not ended everywhere
    assert member(with_option, lower=day_start + 1) is None    # the day may have begun before the window
    assert member(with_option, row={**row, "content_nsfw": 1}) is None
    assert member(with_option, precision="second")[0]["event_at"] is None
