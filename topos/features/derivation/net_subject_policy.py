"""Who, if anyone, this node may hold derived facts about besides its owner.

The derivation writer can route an `about=other:<name>` assertion onto that
person's entity and write a durable fact there. That capability shipped before
anything authorised it, and for a while the only thing preventing third-party
dossiers was that person-name resolution happened to fail. This module is the
authorisation.

Three gates, all of which must open, and each of which fails closed:

  1. **The pack** must declare `net_subject: allow`. Default deny, so a pack
     written for the owner cannot start describing other people because someone
     enabled it.
  2. **The subject** must be explicitly opted in here. Absence is denial —
     "off until asked" means a person nobody has decided about is not a subject,
     and that includes every person the node has never been asked about.
  3. **The subject must not be black-holed.** The blackhole was previously
     consulted only when reading, so a person the owner had excluded could still
     silently accrue facts that were merely hidden afterwards. Excluding someone
     should stop the node thinking about them, not just stop it talking.

A missing policy table denies everything. That is deliberate: the code ships
before the migration that creates the table, and during that window the correct
behaviour is to write nothing outward.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

TABLE = "net_subject_policy"

#: The only value that authorises an outward write. Anything else — including a
#: row that says 'deny', a missing row, or a missing table — refuses.
ALLOW = "allow"
DENY = "deny"


class Decision:
    """Why a subject was allowed or refused. The reason is the product: a
    quarantine row that cannot say which gate closed is not reviewable."""

    __slots__ = ("allowed", "reason")

    def __init__(self, allowed: bool, reason: str) -> None:
        self.allowed = allowed
        self.reason = reason

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.allowed

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"Decision(allowed={self.allowed}, reason={self.reason!r})"


def _table_exists(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False


def _blackhole_table_exists(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_blackholes'"
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False


def subject_policy(conn: sqlite3.Connection, subject_entity_id: str) -> Optional[str]:
    """The recorded decision for this subject, or None when nobody has decided."""
    sid = str(subject_entity_id or "").strip()
    if not sid or not _table_exists(conn):
        return None
    try:
        row = conn.execute(
            f"SELECT policy FROM {TABLE} WHERE subject_entity_id=?", (sid,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]).strip().lower() if row and row[0] else None


def may_write_about(
    conn: sqlite3.Connection,
    subject_entity_id: str,
    *,
    pack_allows_net_subject: bool,
) -> Decision:
    """Decide whether a fact may be written about a non-owner subject.

    Order is chosen so the reason names the FIRST thing that would have to change
    for the answer to become yes — which is what makes a quarantine row actionable.
    """
    sid = str(subject_entity_id or "").strip()
    if not sid:
        return Decision(False, "net_subject_no_subject")

    if not pack_allows_net_subject:
        return Decision(False, "net_subject_pack_denies")

    if not _table_exists(conn):
        # The migration has not landed. Deny, and say so distinctly: an operator
        # reading "policy table absent" knows to ship the migration, whereas
        # "not opted in" would send them hunting for a decision to make.
        return Decision(False, "net_subject_policy_absent")

    policy = subject_policy(conn, sid)
    if policy != ALLOW:
        return Decision(False, "net_subject_not_opted_in")

    # Last, because it is the most expensive check and the most absolute answer.
    #
    # The table's existence is checked HERE rather than trusted to the store.
    # `BlackholeStore.is_blackholed` returns False when the table is missing —
    # correct for its original read-side job, where a node without the feature
    # should not filter everything away, and exactly backwards as an
    # authorisation: "the exclusion list is missing" must never read as "you may
    # write about this person". Its docstring says it fails closed; for a write
    # gate it does not, so this does.
    try:
        if not _blackhole_table_exists(conn):
            return Decision(False, "net_subject_blackhole_unreadable")
        from ..lifecycle.blackhole import BlackholeStore

        if BlackholeStore(conn).is_blackholed(sid):
            return Decision(False, "net_subject_blackholed")
    except Exception:  # noqa: BLE001 — any failure to CHECK is a refusal
        return Decision(False, "net_subject_blackhole_unreadable")

    return Decision(True, "net_subject_allowed")


def set_subject_policy(
    conn: sqlite3.Connection,
    subject_entity_id: str,
    policy: str,
    *,
    decided_by: str = "owner",
    note: str = "",
) -> None:
    """Record the owner's decision about one subject. Only the owner calls this."""
    sid = str(subject_entity_id or "").strip()
    pol = str(policy or "").strip().lower()
    if not sid:
        raise ValueError("subject_entity_id is required")
    if pol not in (ALLOW, DENY):
        raise ValueError(f"policy must be {ALLOW!r} or {DENY!r}, got {policy!r}")
    if not _table_exists(conn):
        raise RuntimeError(
            f"{TABLE} does not exist — run migrations before recording a net-subject decision"
        )
    conn.execute(
        f"""
        INSERT INTO {TABLE} (subject_entity_id, policy, decided_by, note, decided_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(subject_entity_id) DO UPDATE SET
            policy=excluded.policy, decided_by=excluded.decided_by,
            note=excluded.note, decided_at=excluded.decided_at
        """,
        (sid, pol, str(decided_by or "owner"), str(note or "")),
    )
