"""Per-subject authorisation for derived facts about people other than the owner.

NOT REGISTERED YET — deliberately, and the rule is general: a migration registers
in the SAME release that ships it, never before. Registering ahead of a release
once bumped the repo's migration head past the installed engine, an editable
import applied it to the live database, and the downgrade guard fenced the node
out of every write for ~25 minutes (2026-08-25).

TO LAND: add `_spec(<next free order>, NET_SUBJECT_POLICY_V1_ID,
apply_net_subject_policy_v1_up)` to registry.py as part of an engine release.

Nothing depends on this table existing. `net_subject_policy.may_write_about`
treats an absent table as a refusal with the distinct reason
`net_subject_policy_absent`, so the code ships safely before the migration and
simply writes nothing outward in the meantime — which is the correct behaviour
for a consent plane that has not been installed.

Absence of a ROW is also a refusal. The owner's standing decision is "off until
asked", so a person nobody has ruled on is not a subject.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "net_subject_policy_v1"


def apply_net_subject_policy_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS net_subject_policy (
            subject_entity_id TEXT PRIMARY KEY,
            policy TEXT NOT NULL CHECK (policy IN ('allow', 'deny')),
            decided_by TEXT NOT NULL DEFAULT 'owner',
            note TEXT NOT NULL DEFAULT '',
            decided_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # The only read this serves is "may I write about this one subject", which the
    # primary key already answers. The partial index exists for the owner-facing
    # question "who have I opted in", which should stay cheap as the table grows.
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_net_subject_policy_allowed
        ON net_subject_policy (subject_entity_id) WHERE policy = 'allow'
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
