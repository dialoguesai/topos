"""Who, if anyone, this node may hold derived facts about besides its owner.

The derivation writer can route an `about=other:<name>` assertion onto that
person's entity and write a durable fact there. That capability shipped before
anything authorised it, and for a while the only thing preventing third-party
dossiers was that person-name resolution happened to fail. This module is the
authorisation.

Three gates, all of which must open, and each of which fails closed:

  1. **The pack** must declare `net_subject: allow`. Default deny, so a pack
     written for the owner cannot start describing other people because someone
     enabled it. No shipped pack declares it, so this gate alone holds the whole
     lane closed today.
  2. **The subject must be nameable, or explicitly allowed.** Revised 2026-08-26
     (owner): the earlier posture was "off until asked", which made every third
     party a manual decision and put a human in front of a lane that is supposed to
     be automatic. The default is now a RULE rather than a list — a person the node
     can actually name is a subject; a bare phone number with no display name is
     not. Measured on the live node the day the rule was written: 1,351 of 1,505
     person entities nameable (89.8%), 154 excluded, and every excluded one is a
     raw `+1…` with no contact behind it. The rationale is as much quality as
     privacy: a dossier keyed to `+15551234567` is not intelligence.

     This table did not disappear; it changed job. It now holds OVERRIDES, and an
     explicit row wins in **both** directions — `deny` excludes someone the rule
     would have admitted, `allow` admits someone it would not.
  3. **The subject must not be black-holed.** The blackhole was previously
     consulted only when reading, so a person the owner had excluded could still
     silently accrue facts that were merely hidden afterwards. Excluding someone
     should stop the node thinking about them, not just stop it talking. It still
     outranks everything above, including an explicit `allow`.

A missing policy table now means "no overrides recorded", not "deny everything" —
the default rule needs no table. The consequence to keep in view: until the
migration lands, the owner cannot record a `deny`, so the only way to exclude a
nameable person is the blackhole.
"""

from __future__ import annotations

import re
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


#: Two or more consecutive letters — enough to tell "Tango Uniform" or a handle from
#: "+1 (512) 633-1615". Deliberately loose: the rule is meant to exclude the unnameable,
#: not to adjudicate what counts as a real name.
_LETTERS = re.compile(r"[A-Za-z]{2,}")
_PHONE_ONLY = re.compile(r"^[\s+()\-.\d]+$")

#: Opaque machine identifiers that PASS the letter test because hex digits include a-f.
#: Measured 2026-08-26, the day the rule shipped: 257 of the 1,351 subjects it authorised
#: were bare UUIDs like '187d819a-6f9f-4890-a380-099779a0ebef' — 19%, every one of them a
#: dossier keyed to an opaque id, which is precisely what this rule exists to prevent. A
#: name made only of [0-9a-f] and 12+ characters long is not a name anyone was given.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_RUN = re.compile(r"^[0-9a-f]{12,}$", re.I)
#: Scheme-prefixed ids, one or more segments: `ent_<hex>`, `test-dataset:contact:<hex>`.
#: Names do not contain colons or underscores followed by a long hex run.
_SCHEMED_ID = re.compile(
    r"^[a-z][a-z0-9_.\-]*(?:[:_][a-z0-9_.\-]+)*[:_][0-9a-f]{8,}$", re.I)


def _is_opaque_id(t: str) -> bool:
    return bool(_UUID.fullmatch(t) or _HEX_RUN.fullmatch(t) or _SCHEMED_ID.fullmatch(t))


def _looks_named(text: Optional[str]) -> bool:
    t = str(text or "").strip()
    if not t or _PHONE_ONLY.fullmatch(t) or _is_opaque_id(t):
        return False
    if "@" in t:
        return True
    if not _LETTERS.search(t):
        return False
    # A phone number with a telephony crumb attached — "+1512633 ext", "512-4361 x2" — is
    # still a phone number. When digits dominate and the letters amount to a short single
    # token, the letters are annotation, not identity.
    digits = sum(ch.isdigit() for ch in t)
    letters = sum(ch.isalpha() for ch in t)
    if digits >= 7 and letters <= 3:
        return False
    return True


def is_nameable_subject(conn: sqlite3.Connection, subject_entity_id: str) -> bool:
    """Can the node actually name this person?

    Two places carry a name, and both count: the entity's own `canonical_name`, and the
    `display_name` of the contact it is linked to. The contact LINK on its own proves
    nothing — measured 2026-08-26, 1,249 of 1,505 person entities carry a `contact_id`
    including every bare phone number, because a contact row is minted for each messenger
    participant whether or not anyone ever named them. Checking the link instead of the
    name admitted 100% of entities, which is how this rule got caught being no rule at all.

    An unknown subject, or a database without `entities`, is not nameable. Failing closed
    here is the whole point: this function decides who may accrue a dossier.
    """
    sid = str(subject_entity_id or "").strip()
    if not sid:
        return False
    try:
        row = conn.execute(
            "SELECT e.canonical_name, ("
            "  SELECT c.display_name FROM contacts c WHERE c.contact_id = e.contact_id"
            ") FROM entities e WHERE e.entity_id = ?", (sid,)).fetchone()
    except sqlite3.Error:
        # No entities table, or no contacts table to join. Either way we cannot establish
        # a name, and "we could not check" must never read as "yes".
        try:
            row = conn.execute(
                "SELECT canonical_name, NULL FROM entities WHERE entity_id = ?", (sid,)).fetchone()
        except sqlite3.Error:
            return False
    if not row:
        return False
    return _looks_named(row[0]) or _looks_named(row[1])


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

    # An explicit decision outranks the rule in BOTH directions. `subject_policy`
    # returns None when the table is absent, which is now an ordinary state: the table
    # holds overrides, and having none is not the same as denying everyone.
    policy = subject_policy(conn, sid)
    if policy == DENY:
        return Decision(False, "net_subject_opted_out")
    if policy != ALLOW and not is_nameable_subject(conn, sid):
        # The refusal names what would have to change: give this person a name, or
        # record an explicit allow. "not opted in" would have sent the owner hunting
        # for a decision that the rule is supposed to make for them.
        return Decision(False, "net_subject_unnamed_subject")

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


def is_owner_entity(conn: sqlite3.Connection, entity_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM entities WHERE entity_id=? AND is_self=1", (str(entity_id or ""),)
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row)


def may_owner_write_about(conn: sqlite3.Connection, subject_entity_id: str) -> Decision:
    """The owner's own hand — promoting a quarantined fact, or editing one, onto a subject.

    D-E (owner, 2026-08-26): *the owner deciding to put a note on someone's card IS the
    consent decision.* So this path deliberately skips gate 1 (does the pack allow outward
    writes) and gate 2 (can the node name this person) — the owner has just supplied, by
    hand, the judgement those two gates exist to approximate. Requiring a pack to authorise
    what a human explicitly typed would make the review queue unusable.

    What it does NOT skip is the blackhole. That gate exists because the blackhole was once
    a read-side filter only, so a person the owner had excluded kept accruing facts that
    were merely hidden afterwards. "Forget this person" has to outrank every actor
    including the one allowed through everything else — otherwise the only thing standing
    between an exclusion and its reversal is the owner remembering they made it.
    """
    sid = str(subject_entity_id or "").strip()
    if not sid:
        return Decision(False, "net_subject_no_subject")
    if is_owner_entity(conn, sid):
        return Decision(True, "owner_subject")
    try:
        if not _blackhole_table_exists(conn):
            return Decision(False, "net_subject_blackhole_unreadable")
        from ..lifecycle.blackhole import BlackholeStore

        if BlackholeStore(conn).is_blackholed(sid):
            return Decision(False, "net_subject_blackholed")
    except Exception:  # noqa: BLE001 — any failure to CHECK is a refusal
        return Decision(False, "net_subject_blackhole_unreadable")
    return Decision(True, "owner_directed")


def record_owner_decision(conn: sqlite3.Connection, subject_entity_id: str,
                          *, note: str = "") -> bool:
    """Write down what the owner's action already implied.

    Best-effort on purpose: the migration is not registered yet, and a promotion must not
    fail because the override table does not exist. Returns whether it was recorded, so a
    caller can report "stored, but the decision was not durably noted" rather than imply
    a consent record exists that does not.
    """
    try:
        set_subject_policy(conn, subject_entity_id, ALLOW, decided_by="owner",
                           note=note or "recorded from an owner promotion")
        return True
    except (RuntimeError, sqlite3.Error, ValueError):
        return False


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
