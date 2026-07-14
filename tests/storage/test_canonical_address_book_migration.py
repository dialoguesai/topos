from __future__ import annotations

import json
import sqlite3

from topos.sources.definitions import CANONICAL_ADDRESS_BOOK_SOURCE_ID
from topos.storage.db.migrations.canonical_address_book_v1 import (
    MIGRATION_ID,
    apply_canonical_address_book_v1_up,
)
from topos.storage.db.migrations.wiki_mvp_phase0 import apply_wiki_mvp_phase0_up
from topos.storage.canonical.conversations_tables import (
    ensure_contact_identifiers_table,
    ensure_contacts_table,
)


def test_migration_preserves_contacts_and_removes_only_legacy_manifest() -> None:
    conn = sqlite3.connect(":memory:")
    apply_wiki_mvp_phase0_up(conn)
    ensure_contacts_table(conn)
    ensure_contact_identifiers_table(conn)
    conn.execute(
        """
        CREATE TABLE source_runtime_installs (
            install_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            source_definition_json TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO contacts (
            contact_id, dataset_id, source_id, display_name, known_usernames_json,
            is_self, created_at, updated_at
        ) VALUES (?, 'dataset-1', 'global', ?, '[]', 0, datetime('now'), datetime('now'))
        """,
        [(f"contact-{index}", f"Person {index}") for index in range(3)],
    )
    conn.execute(
        """
        INSERT INTO contact_identifiers (
            dataset_id, source_id, identifier, identifier_type, contact_id, created_at, updated_at
        ) VALUES ('dataset-1', 'imessage', '+15550001', 'phone', 'contact-1', datetime('now'), datetime('now'))
        """
    )
    legacy_definition = {
        "source_id": "global",
        "display_name": "Address Book (merged)",
        "default_scope_id": "contacts",
        "allowed_scope_ids": ["contacts:resolve", "relationship_context:read"],
    }
    unrelated_definition = {
        "source_id": "global",
        "display_name": "Unrelated complete source",
        "source_type": "ui_stream",
        "schema_id": "other.v1",
        "parser_id": "other.v1",
    }
    conn.executemany(
        "INSERT INTO source_runtime_installs VALUES (?, 'global', ?)",
        [
            ("legacy", json.dumps(legacy_definition)),
            ("unrelated", json.dumps(unrelated_definition)),
        ],
    )
    conn.commit()

    result = apply_canonical_address_book_v1_up(conn)

    assert result == {"contacts_migrated": 3, "legacy_installs_removed": 1}
    assert conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE source_id=?",
        (CANONICAL_ADDRESS_BOOK_SOURCE_ID,),
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT source_id FROM contact_identifiers WHERE contact_id='contact-1'"
    ).fetchone()[0] == "imessage"
    assert conn.execute(
        "SELECT install_id FROM source_runtime_installs ORDER BY install_id"
    ).fetchall() == [("unrelated",)]
    assert conn.execute(
        "SELECT COUNT(*) FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()[0] == 1

    assert apply_canonical_address_book_v1_up(conn) == {
        "contacts_migrated": 0,
        "legacy_installs_removed": 0,
    }

