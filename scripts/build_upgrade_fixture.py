#!/usr/bin/env python3
"""Build a SQLite fixture DB that looks like a node stuck on an older baseline.

PLAN_NODE_RELEASE_MIGRATIONS M4 — upgrade-matrix fixture builder.

Two modes:

  --from-current (CI default)
      Uses the *current checkout* to apply the full migration chain, inserts a
      few synthetic coverage rows with NULL ``spec_version`` (pre-stamp era),
      and stamps ``engine.upgrade.baseline`` to ``--version``. The result is an
      "as if upgraded from X.Y.Z" database that current code can open and catch up.

Both modes seed real canonical conversation rows for a registry source plus the
matching ``timeline`` rows. That seed is load-bearing, not decoration: the
upgrade runner discovers work through ``_real_source_ids()`` (which reads
``timeline``), so a fixture without it makes every enrichment step a silent
no-op that still ledgers "done". See ``seed_canonical_source`` below.

Both modes also seed invented contacts that carry the older sharing model's
stored per-person choice (``CONTACT_CHOICES``, ``seed_contact_choices``). The
1.5.0 step ``carry-contact-excludes-to-off-limits`` acts only on such rows, so
without them it too would ledger "done" having done nothing.

  (default / PyPI mode)
      Creates a venv, ``pip install topos-node==VERSION``, and boots enough of
      that package to apply migrations. Prefer this for nightly when old wheels
      remain installable; fall back to ``--from-current`` when wheels are heavy
      or flaky.

Usage:
  python scripts/build_upgrade_fixture.py --from-current --version 1.3.2 \\
      --out /tmp/upgrade-fixture.db
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

# Seed rows deliberately omit spec_version (NULL) so stale-predicate paths stay
# exercisable after ensure_migrations_applied adds the column.
_SEED_SQL = """
INSERT OR REPLACE INTO message_entities (
    entity_id, record_id, source_id, entity_text, model, provider, payload_json
) VALUES (
    'fixture-e1', 'fixture-m1', 'fixture_src', 'Alice', 'fixture', 'test', '{}'
);
INSERT OR REPLACE INTO message_topics (
    topic_id, record_id, source_id, topic, model, provider, payload_json
) VALUES (
    'fixture-t1', 'fixture-m1', 'fixture_src', 'work', 'fixture', 'test', '{}'
);
INSERT OR REPLACE INTO message_emotions (
    emotion_id, record_id, source_id, model, provider, payload_json
) VALUES (
    'fixture-em1', 'fixture-m1', 'fixture_src', 'fixture', 'test', '{}'
);
INSERT OR REPLACE INTO entities (
    entity_id, entity_type, canonical_name, normalized_name, is_self
) VALUES (
    'fixture-ent1', 'person', 'Alice', 'alice', 0
);
"""


# A fixture with no canonical rows is worse than no fixture: the upgrade
# runner's enrichment executor walks _real_source_ids(), which reads
# `timeline`. An empty timeline means every enrichment_reprocess step ledgers
# "done" with {"sources": {}} — steps_run counts up, the matrix goes green, and
# nothing was exercised (observed 2026-08-07). So seed a REAL registry source
# with canonical messages plus the timeline rows that advertise it.
#
# voxterm_transcripts is chosen deliberately: it is in topos.sources.registry
# (so _process_enrichment_core resolves it instead of raising ValueError ->
# "unknown_source_skipped") and its id does not start with any prefix that
# _real_source_ids skips (demo_/enrichment_lab/sanity/test/manual_enrichment).
_FIXTURE_SOURCE_ID = "voxterm_transcripts"
_FIXTURE_CONVERSATION_ID = "upgrade-fixture-c1"

# Content carries unambiguous PERSON/ORG/GPE surfaces so the NER pass has
# something to find; an extraction that returns nothing would be
# indistinguishable from an extraction that never ran.
_FIXTURE_MESSAGES = (
    (
        "upgrade-fixture-m1", "user", "self", 1,
        "Met Alice Johnson at the Berlin office to plan the Q3 migration.",
        "2026-05-01T10:00:00+00:00",
    ),
    (
        "upgrade-fixture-m2", "contact", "alice", 0,
        "Bob Carter from Acme Corp will join us in Munich next Tuesday.",
        "2026-05-01T10:05:00+00:00",
    ),
    (
        "upgrade-fixture-m3", "user", "self", 1,
        "I told Carol Nguyen that Topos ships the graph rebuild this week.",
        "2026-05-02T09:00:00+00:00",
    ),
)

# Minimal shapes matching topos.storage.canonical.conversations_tables. Written
# as plain SQL so both build modes (current checkout AND an old PyPI wheel) can
# seed identically; the migration chain then evolves them (actor_role_v1 adds
# and backfills actor_role over these rows, which is itself worth covering).
_CANONICAL_DDL = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    source_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (conversation_id, dataset_id)
);
CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id TEXT NOT NULL PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    sender_type TEXT,
    sender_id TEXT,
    reply_to_message_id TEXT,
    message_type TEXT,
    event_type TEXT,
    content TEXT,
    event_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    is_from_self INTEGER DEFAULT 0,
    owner_user_id TEXT
);
"""


def seed_canonical_source(conn: sqlite3.Connection) -> None:
    """Create + populate canonical conversation rows for the fixture source.

    Runs BEFORE the migration chain so the fixture mirrors a real pre-1.2.0
    node: canonical data already on disk, schema evolved underneath it.
    """
    conn.executescript(_CANONICAL_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO conversations (conversation_id, dataset_id, source_id) "
        "VALUES (?, 'default', ?)",
        (_FIXTURE_CONVERSATION_ID, _FIXTURE_SOURCE_ID),
    )
    for message_id, sender_type, sender_id, is_from_self, content, event_at in _FIXTURE_MESSAGES:
        conn.execute(
            "INSERT OR REPLACE INTO conversation_messages "
            "(message_id, conversation_id, dataset_id, sender_type, sender_id, "
            " content, event_at, source_id, is_from_self, message_type) "
            "VALUES (?, ?, 'default', ?, ?, ?, ?, ?, ?, 'text')",
            (
                message_id, _FIXTURE_CONVERSATION_ID, sender_type, sender_id,
                content, event_at, _FIXTURE_SOURCE_ID, is_from_self,
            ),
        )
    conn.commit()


def seed_timeline(conn: sqlite3.Connection) -> None:
    """Advertise the fixture source in `timeline` (post-migration: it creates it).

    _real_source_ids() reads ONLY this table — without these rows the enrichment
    executors have no source list to walk.
    """
    for message_id, _st, _sid, _self, _content, event_at in _FIXTURE_MESSAGES:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO timeline "
                "(event_at, record_id, source_id, canonical_table, record_type) "
                "VALUES (?, ?, ?, 'conversation_messages', 'message')",
                (event_at, message_id, _FIXTURE_SOURCE_ID),
            )
        except sqlite3.Error as exc:
            print(f"timeline seed skipped ({exc}) for {message_id}", flush=True)
    conn.commit()


# --- the older sharing model's per-person choices -----------------------------
#
# Before 1.5.0 a contact row could carry the owner's stored choice in
# contacts.sharing_policy_json: the app's Include / Exclude toggle
# (row_visibility) and its Yes / Hidden toggle (name_visibility). The 1.5.0
# step carry-contact-excludes-to-off-limits turns every EXPLICIT exclude into
# an Off-limits entry and leaves every other stored value alone
# (topos/features/lifecycle/contact_excludes.py). A fixture with no such
# contact lets the step ledger "done" having done nothing: the trap this
# file's header describes, in a different table.
#
# So the fixture carries one contact for each thing the step has to tell
# apart, and scripts/run_upgrade_matrix.py asserts on this table: an entry for
# every CARRIED row and for no other.
#
# Every person, handle and id here is invented. A handle is an @-name or an
# address under a reserved top-level name; none is a number. Every name is
# long and unlike the fixture's messages on purpose: the Off-limits rebuild
# withdraws derived text that CONTAINS a protected name, so a short alias
# would withdraw rows the older steps' assertions count.

CARRIED = "explicit_exclude"
NOT_CARRIED = ("explicit_include", "hidden_name", "no_stored_choice", "unreadable", "no_row_choice")
#: How contact_excludes._entry names an entry, in the order it tries them.
NAMING_BRANCHES = ("linked_entity", "name", "handle", "contact_id_only")

_CONTACT_DATASET_ID = "default"
_CONTACT_SOURCE_ID = "upgrade_fixture_contacts"
# Written as the older model's own writer wrote them
# (ConversationsTablesManager.update_contact_sharing_policy: json.dumps of the two keys).
_EXCLUDE = json.dumps({"name_visibility": "normal", "row_visibility": "exclude_from_grants"})
_HIDDEN_EXCLUDE = json.dumps({"name_visibility": "hidden", "row_visibility": "exclude_from_grants"})
_INCLUDE = json.dumps({"name_visibility": "normal", "row_visibility": "normal"})
_HIDDEN_INCLUDE = json.dumps({"name_visibility": "hidden", "row_visibility": "normal"})


class ContactChoice(NamedTuple):
    """One invented contact and what the carry step must do with it."""

    contact_id: str
    case: str  # CARRIED, or one of NOT_CARRIED
    stored_choice: Optional[str]  # contacts.sharing_policy_json, byte for byte (None: never set)
    display_name: Optional[str] = None
    usernames: Tuple[str, ...] = ()
    handles: Tuple[Tuple[str, str], ...] = ()  # (identifier, identifier_type)
    # (entity_id, canonical_name, aliases, mention_count) of each entity linked to the contact
    entities: Tuple[Tuple[str, str, Tuple[str, ...], int], ...] = ()
    named_by: Optional[str] = None  # a carried contact's naming branch (NAMING_BRANCHES)


CONTACT_CHOICES: Tuple[ContactChoice, ...] = (
    # --- explicit excludes: each becomes exactly one Off-limits entry ---------
    # Linked entity, with a display name, a username and a handle beside it:
    # named by the entity, everything else becomes an alias.
    ContactChoice(
        "upgrade-fixture-contact-entity", CARRIED, _EXCLUDE,
        display_name="Ysolde M.",
        usernames=("ysolde_marchbank",),
        handles=(("ysolde.marchbank@example.invalid", "email"),),
        entities=(("upgrade-fixture-entity-ysolde", "Ysolde Marchbank", ("Ysoldina",), 3),),
        named_by="linked_entity",
    ),
    # Two linked entities and no display name: the most mentioned one names the
    # entry. Its id sorts LAST, so an ordering by id alone picks the other.
    ContactChoice(
        "upgrade-fixture-contact-two-entities", CARRIED, _EXCLUDE,
        handles=(("@perpetua.lindqvist", "username"),),
        entities=(
            ("upgrade-fixture-entity-z-perpetua", "Perpetua Lindqvist", (), 5),
            ("upgrade-fixture-entity-a-perpetua", "Pia Lindqvist", (), 2),
        ),
        named_by="linked_entity",
    ),
    # A display name and nothing else.
    ContactChoice(
        "upgrade-fixture-contact-name", CARRIED, _EXCLUDE,
        display_name="Corwin Athelney",
        named_by="name",
    ),
    # A display name with a handle and a username: still named by the name.
    ContactChoice(
        "upgrade-fixture-contact-name-handle", CARRIED, _EXCLUDE,
        display_name="Odalys Fenwright",
        usernames=("odalys_fenwright",),
        handles=(("@odalys.fenwright", "username"),),
        named_by="name",
    ),
    # Name hidden AND rows excluded: the exclude is what is carried.
    ContactChoice(
        "upgrade-fixture-contact-hidden-exclude", CARRIED, _HIDDEN_EXCLUDE,
        display_name="Evander Thistlewood",
        named_by="name",
    ),
    # No display name, two handles: named by the first handle.
    ContactChoice(
        "upgrade-fixture-contact-handle", CARRIED, _EXCLUDE,
        handles=(
            ("tobiah.quillon@example.invalid", "email"),
            ("@tobiah.q", "username"),
        ),
        named_by="handle",
    ),
    # A username is an alias, never a name: with nothing else the contact id names the entry.
    ContactChoice(
        "upgrade-fixture-contact-username-only", CARRIED, _EXCLUDE,
        usernames=("wrenna_ostrander",),
        named_by="contact_id_only",
    ),
    # Nothing but the row itself.
    ContactChoice(
        "upgrade-fixture-contact-bare", CARRIED, _EXCLUDE,
        named_by="contact_id_only",
    ),
    # --- everything else: no Off-limits entry ---------------------------------
    # A stored choice that is not an exclude. It has a linked entity and a
    # handle, so a step that carried it would have everything it needs.
    ContactChoice(
        "upgrade-fixture-contact-include", "explicit_include", _INCLUDE,
        display_name="Leocadia Brightwater",
        handles=(("@leocadia.brightwater", "username"),),
        entities=(("upgrade-fixture-entity-leocadia", "Leocadia Brightwater", (), 4),),
    ),
    # Name hidden, rows included: counted by the step, not carried.
    ContactChoice(
        "upgrade-fixture-contact-hidden", "hidden_name", _HIDDEN_INCLUDE,
        display_name="Ignatius Farrowdale",
    ),
    # No stored choice: excluded only by the older model's default.
    ContactChoice(
        "upgrade-fixture-contact-default", "no_stored_choice", None,
        display_name="Marisol Quennevere",
        handles=(("marisol.quennevere@example.invalid", "email"),),
    ),
    # A stored value that is blank: no stored choice either.
    ContactChoice(
        "upgrade-fixture-contact-blank", "no_stored_choice", "  ",
        display_name="Philippa Stonemere",
    ),
    # A stored value that does not parse.
    ContactChoice(
        "upgrade-fixture-contact-unreadable", "unreadable", "{not json",
        display_name="Barnaby Elderwick",
    ),
    # A stored value that parses but is not an object, and says "exclude".
    ContactChoice(
        "upgrade-fixture-contact-not-an-object", "unreadable", json.dumps("exclude_from_grants"),
        display_name="Seraphine Oakhollow",
    ),
    # A stored object with no row choice at all.
    ContactChoice(
        "upgrade-fixture-contact-no-row-choice", "no_row_choice", json.dumps({"name_visibility": "hidden"}),
        display_name="Lucan Wetherby",
    ),
)

# Shapes of topos.storage.db.migrations.wiki_mvp_phase1, for an old wheel whose
# chain does not create them yet. On a current chain these are no-ops.
_CONTACTS_DDL = """
CREATE TABLE IF NOT EXISTS contacts (
    contact_id TEXT NOT NULL PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    display_name TEXT,
    known_usernames_json TEXT,
    is_self INTEGER NOT NULL DEFAULT 0,
    source_record_id TEXT,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    sync_batch_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS contact_identifiers (
    dataset_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    identifier TEXT NOT NULL,
    identifier_type TEXT,
    contact_id TEXT NOT NULL,
    source_record_id TEXT,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    sync_batch_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (dataset_id, source_id, identifier)
);
"""


def seed_contact_choices(conn: sqlite3.Connection, *, strict: bool = True) -> None:
    """Write CONTACT_CHOICES: the contacts, their handles and their linked entities.

    Runs AFTER the migration chain, which creates ``contacts`` and ``entities``.
    ``sharing_policy_json`` is not in any migration: the older model's own
    tables manager added the column the first time it ran
    (conversations_tables._ensure_contact_sharing_policy_column), so a node that
    ever stored a choice has it, and it is added here the same way.

    ``strict`` is for the current chain, where a failed insert means this
    builder is wrong. An old wheel's ``entities`` may lack ``contact_id``; then
    the linked entities are skipped with a line saying so, and the matrix
    refuses the fixture rather than passing a naming branch it never ran.
    """
    conn.executescript(_CONTACTS_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(contacts)")}
    if "sharing_policy_json" not in columns:
        conn.execute("ALTER TABLE contacts ADD COLUMN sharing_policy_json TEXT")
    for choice in CONTACT_CHOICES:
        conn.execute(
            "INSERT OR REPLACE INTO contacts "
            "(contact_id, dataset_id, source_id, display_name, known_usernames_json, sharing_policy_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                choice.contact_id, _CONTACT_DATASET_ID, _CONTACT_SOURCE_ID, choice.display_name,
                json.dumps(list(choice.usernames)) if choice.usernames else None,
                choice.stored_choice,
            ),
        )
        for identifier, identifier_type in choice.handles:
            conn.execute(
                "INSERT OR REPLACE INTO contact_identifiers "
                "(dataset_id, source_id, identifier, identifier_type, contact_id) VALUES (?, ?, ?, ?, ?)",
                (_CONTACT_DATASET_ID, _CONTACT_SOURCE_ID, identifier, identifier_type, choice.contact_id),
            )
        for entity_id, canonical_name, aliases, mention_count in choice.entities:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO entities "
                    "(entity_id, entity_type, canonical_name, normalized_name, aliases_json, "
                    " is_self, contact_id, mention_count) VALUES (?, 'person', ?, ?, ?, 0, ?, ?)",
                    (
                        entity_id, canonical_name, canonical_name.lower(), json.dumps(list(aliases)),
                        choice.contact_id, mention_count,
                    ),
                )
            except sqlite3.Error as exc:
                if strict:
                    raise
                print(f"contact seed skipped ({exc}): linked entity of {choice.contact_id}", flush=True)
    conn.commit()


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env=env)


def _stamp_baseline(conn: sqlite3.Connection, version: str) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS engine_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO engine_config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("engine.upgrade.baseline", version),
    )
    conn.commit()


def _seed_coverage(conn: sqlite3.Connection) -> None:
    """Insert synthetic rows; leave spec_version unset/NULL when the column exists."""
    # Prefer INSERT without spec_version so NULL stamps survive if the column is present.
    for stmt in _SEED_SQL.strip().split(";"):
        sql = stmt.strip()
        if not sql:
            continue
        try:
            conn.execute(sql)
        except sqlite3.Error as exc:
            # Older schemas may lack a table; skip non-critical seeds.
            print(f"seed skipped ({exc}): {sql[:60]}...", flush=True)
    # Explicit NULL if column exists (idempotent).
    try:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(message_entities)").fetchall()
        }
        if "spec_version" in cols:
            conn.execute(
                "UPDATE message_entities SET spec_version=NULL "
                "WHERE entity_id LIKE 'fixture-%'"
            )
    except sqlite3.Error:
        pass
    conn.commit()


def build_from_current(version: str, out: Path) -> None:
    """Apply current-checkout migrations, seed rows, stamp baseline to *version*."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from topos.storage.db.migrations import apply_all_migrations

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    conn = sqlite3.connect(str(out))
    try:
        # Canonical data first: a real pre-1.2.0 node has rows on disk before
        # the chain runs, and actor_role_v1's backfill then has something to
        # walk instead of an empty table.
        seed_canonical_source(conn)
        apply_all_migrations(conn)
        _seed_coverage(conn)
        seed_timeline(conn)  # `timeline` is created by the chain, so seed after
        seed_contact_choices(conn)  # `contacts` and `entities` likewise
        _stamp_baseline(conn, version)
    finally:
        conn.close()

    print(
        f"from-current fixture written: {out} "
        f"(baseline={version}; schema=current; coverage rows spec_version=NULL; "
        f"{len(_FIXTURE_MESSAGES)} canonical rows for {_FIXTURE_SOURCE_ID}; "
        f"{len(CONTACT_CHOICES)} contacts, "
        f"{sum(1 for c in CONTACT_CHOICES if c.case == CARRIED)} with an explicit exclude)",
        flush=True,
    )
    print(
        "Note: nightly can use PyPI mode (omit --from-current) when "
        f"topos-node=={version} wheels remain installable.",
        flush=True,
    )


_PYPI_BOOT_SCRIPT = textwrap.dedent(
    """\
    import os
    import sqlite3
    import sys

    db_path = sys.argv[1]
    version = sys.argv[2]
    os.environ["TOPOS_DATABASE_PATH"] = db_path
    os.environ.setdefault("TOPOS_SKIP_UPDATE_CHECK", "1")
    os.environ.setdefault("TOPOS_UPGRADE_RUNNER", "off")

    conn = sqlite3.connect(db_path)
    try:
        # Prefer package migration APIs when present.
        try:
            from topos.storage.db.migrations import apply_all_migrations
            apply_all_migrations(conn)
        except Exception:
            from topos.storage.db.migrations import ensure_migrations_applied
            ensure_migrations_applied(conn)
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS engine_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            '''
        )
        conn.execute(
            "INSERT INTO engine_config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("engine.upgrade.baseline", version),
        )
        # Best-effort coverage seed (schema may differ by version).
        try:
            conn.execute(
                "INSERT OR REPLACE INTO message_entities "
                "(entity_id, record_id, source_id, entity_text, model, provider, payload_json) "
                "VALUES ('fixture-e1', 'fixture-m1', 'fixture_src', 'Alice', 'fixture', 'test', '{}')"
            )
        except Exception as exc:
            print(f"pypi seed skipped: {exc}", flush=True)
        conn.commit()
    finally:
        conn.close()
    print("pypi_fixture_ok", flush=True)
    """
)


def build_from_pypi(version: str, out: Path) -> None:
    """Install topos-node==version in a venv and seed a DB via that package."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    with tempfile.TemporaryDirectory(prefix="topos-upgrade-fixture-") as tmp:
        tmp_path = Path(tmp)
        venv_dir = tmp_path / "venv"
        _run([sys.executable, "-m", "venv", str(venv_dir)])
        python = venv_dir / "bin" / "python"
        if not python.exists():
            python = venv_dir / "Scripts" / "python.exe"

        # Prefer uv pip when available (faster); fall back to python -m pip.
        uv = shutil.which("uv")
        if uv:
            _run([uv, "pip", "install", "--python", str(python), f"topos-node=={version}"])
        else:
            _run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
            _run([str(python), "-m", "pip", "install", f"topos-node=={version}"])

        db_tmp = tmp_path / "fixture.db"
        env = os.environ.copy()
        env["TOPOS_SKIP_UPDATE_CHECK"] = "1"
        env["TOPOS_UPGRADE_RUNNER"] = "off"
        _run(
            [str(python), "-c", _PYPI_BOOT_SCRIPT, str(db_tmp), version],
            env=env,
        )
        shutil.copy2(db_tmp, out)

    # Seed with the CURRENT checkout's SQL rather than the old wheel's helpers
    # (their module paths differ across versions). actor_role and any other
    # missing columns are added by ensure_migrations_applied when the matrix
    # opens this fixture, so post-hoc seeding is equivalent here.
    conn = sqlite3.connect(str(out))
    try:
        seed_canonical_source(conn)
        seed_timeline(conn)
        # The old wheel's own schema: a linked entity it cannot hold is skipped
        # with a line saying so, and the matrix then refuses the fixture.
        seed_contact_choices(conn, strict=False)
    finally:
        conn.close()

    print(
        f"pypi fixture written: {out} (topos-node=={version}; "
        f"{len(_FIXTURE_MESSAGES)} canonical rows for {_FIXTURE_SOURCE_ID}; "
        f"{len(CONTACT_CHOICES)} contacts with a stored sharing choice or none)",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        required=True,
        metavar="X.Y.Z",
        help="Baseline version to stamp (support floor / prior release)",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output path for the fixture SQLite database",
    )
    parser.add_argument(
        "--from-current",
        action="store_true",
        help=(
            "CI-friendly mode: apply current checkout migrations + synthetic "
            "NULL spec_version rows, then stamp baseline to --version "
            "(recommended when installing old PyPI wheels is heavy/flaky)"
        ),
    )
    args = parser.parse_args(argv)

    version = str(args.version).strip().lstrip("v")
    if not version:
        raise SystemExit("--version is required")

    out = args.out.expanduser().resolve()
    if args.from_current:
        build_from_current(version, out)
    else:
        try:
            build_from_pypi(version, out)
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                f"PyPI fixture build failed for topos-node=={version} "
                f"(exit {exc.returncode}). Retry with --from-current for CI."
            ) from exc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
