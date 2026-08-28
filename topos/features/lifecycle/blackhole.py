"""Entity black holes: "this entity is mine alone."

An owner *exclusion* (`exclusions.py`) says "this must not be part of my
intelligence" and answers by deleting. A black hole says the opposite about
retention and the same thing about reach: **keep everything, show it to no one
but me.** The entity, its mentions, its edges and its dossier all stay intact
and fully visible on the owner's own UI; every other caller — the owner's own
third-party MCP agents, routines, grantees, plugins — must be unable to tell the
entity exists at all.

Three contracts are encoded here, straight from the decisions in
`FEASIBILITY_ENTITY_BLACKHOLE.md` §9:

* **D1 — secure processing.** `processing_tier` picks the model set that may
  ever see content mentioning the entity. `secure` admits the local adapters
  plus the Red Pill TEE; `local_only` is the stricter dial. BYOK/OpenAI are
  admitted by neither.
* **D4 — notify first, then rebuild.** Flipping the flag raises a
  `rebuild_needed` notification *before* the rebuild runs, because the hide is
  not yet complete across derived artifacts (briefs, digests, dossiers carry the
  name as a string). While `rebuild_state` is not `complete`, name-string
  artifacts must be withheld from non-owner callers rather than served stale —
  `pending_rebuild_names()` is what the read path asks.
* **D5 — never confirm existence.** Nothing in this module raises "not found"
  differently for a black-holed entity than for one that never existed; callers
  above must preserve that. The store is deliberately silent about *why* a name
  is blocked.

Keyed by normalized name rather than `entity_id`, because a name must be
protectable *before* an entity is minted for it (pre-emptive protection), and
the protection has to survive the entity being re-minted after a merge or a
scrub. `entity_id` rides alongside for the id-join hot path.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional, Sequence, Set

from ...storage.db.write_gate import commit_connection, with_db_write

PROCESSING_TIERS = ("secure", "local_only")
REBUILD_STATES = ("pending", "running", "complete", "failed")
NOTIFICATION_KINDS = (
    "rebuild_needed",
    "rebuild_complete",
    "rebuild_failed",
    "reinclude_needed",
)

# D1: Red Pill's TEE counts as secure. These are the only providers that may
# ever receive content mentioning a black-holed entity. Anything absent from the
# tier's set is refused — the gate fails closed rather than falling back.
TIER_PROVIDERS: Dict[str, frozenset] = {
    "secure": frozenset({"ollama", "huggingface", "redpill"}),
    "local_only": frozenset({"ollama", "huggingface"}),
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _missing_table(exc: sqlite3.OperationalError) -> bool:
    """True when the failure is 'the table isn't there', not 'the DB is unwell'.

    The distinction is load-bearing. A missing table genuinely means nothing has
    ever been black-holed, so an empty set is the correct answer. Any *other*
    operational error (locked, corrupt, disk) must propagate: silently reporting
    "no black holes" on a sick database would fail open, which is exactly the
    failure mode this feature exists to prevent.
    """
    return "no such table" in str(exc).lower()


def normalize_entity_name(name: str) -> str:
    from ..entities.resolver import normalize_name

    return normalize_name(str(name or "").strip())


def _normalized_aliases(aliases_json: Optional[str]) -> List[str]:
    try:
        raw = json.loads(aliases_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for alias in raw:
        norm = normalize_entity_name(str(alias))
        if norm:
            out.append(norm)
    return out


class BlackholeStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -------------------------------------------------------------- reads

    def is_blackholed(self, entity_ref: str) -> bool:
        """True if this entity_id or name is black-holed. Fails closed on a sick DB."""
        ref = str(entity_ref or "").strip()
        if not ref:
            return False
        try:
            row = self._conn.execute(
                "SELECT 1 FROM entity_blackholes WHERE entity_id=? OR normalized_name=?",
                (ref, normalize_entity_name(ref)),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return False
            raise
        return row is not None

    def get(self, entity_ref: str) -> Optional[Dict[str, Any]]:
        ref = str(entity_ref or "").strip()
        if not ref:
            return None
        try:
            row = self._conn.execute(
                """
                SELECT blackhole_id, entity_id, normalized_name, canonical_name,
                       aliases_json, processing_tier, rebuild_state, note, created_at, updated_at
                FROM entity_blackholes WHERE entity_id=? OR normalized_name=?
                """,
                (ref, normalize_entity_name(ref)),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return None
            raise
        return self._row_to_dict(row) if row else None

    def list(self) -> List[Dict[str, Any]]:
        try:
            rows = self._conn.execute(
                """
                SELECT blackhole_id, entity_id, normalized_name, canonical_name,
                       aliases_json, processing_tier, rebuild_state, note, created_at, updated_at
                FROM entity_blackholes ORDER BY created_at DESC
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return []
            raise
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: Sequence[Any]) -> Dict[str, Any]:
        return {
            "blackhole_id": row[0],
            "entity_id": row[1],
            "normalized_name": row[2],
            "canonical_name": row[3],
            "aliases": _normalized_aliases(row[4]),
            "processing_tier": row[5],
            "rebuild_state": row[6],
            "note": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }

    def processing_tier(self, entity_ref: str) -> Optional[str]:
        record = self.get(entity_ref)
        return record["processing_tier"] if record else None

    # ---------------------------------------------------- hot-path lookups

    def blackholed_entity_ids(self) -> Set[str]:
        """Entity ids to exclude from every non-owner read. Empty ids are dropped."""
        try:
            rows = self._conn.execute(
                "SELECT entity_id FROM entity_blackholes WHERE entity_id != ''"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return set()
            raise
        return {str(r[0]) for r in rows if r[0]}

    def blackholed_name_terms(self) -> Set[str]:
        """Normalized names *and* aliases — the belt for the id-join's suspenders.

        Used by the egress alias-scan on high-stakes surfaces (routine email,
        grantee answers) where a mention the resolver never bound would slip past
        an id-only filter.

        Returns the STORED normalization and a FRESH one derived from
        ``canonical_name``, because those can disagree. ``normalized_name`` is
        computed by ``normalize_entity_name`` at write time and then frozen; any
        later change to that function silently invalidates every row written
        before it, and nothing re-derives them.

        Measured on the owner's node 2026-08-27: ``Old Harbor- Rey's Place``
        was flagged on 2026-08-07 and stored as ``old harbor- rey s place``,
        while the current function yields ``old harbor- rey place`` — the
        one-character token is now dropped. ``blocks_name()`` therefore returned
        **False for the entity's own exact name**. Together with the entity row
        having been reaped, that black hole had no protection left at all: the id
        filter was empty and the name filter could not match.

        Emitting both is self-healing without a migration, and it stays correct
        through the next change to the normalizer too.
        """
        try:
            rows = self._conn.execute(
                "SELECT normalized_name, aliases_json, canonical_name FROM entity_blackholes"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return set()
            raise
        terms: Set[str] = set()
        for normalized_name, aliases_json, canonical_name in rows:
            if normalized_name:
                terms.add(str(normalized_name))
            fresh = normalize_entity_name(str(canonical_name or ""))
            if fresh:
                terms.add(fresh)
            terms.update(_normalized_aliases(aliases_json))
        return terms

    def renormalize_stored_names(self) -> int:
        """Re-derive frozen ``normalized_name`` values with the current function.

        The read path above tolerates the drift; the LOOKUP paths cannot, because
        ``get()`` and ``is_blackholed()`` match ``normalized_name = ?`` against a
        freshly-normalized argument. A stale row is therefore unreachable by name
        — the owner cannot even un-blackhole it through the UI.

        Idempotent, and safe to call at startup: it only rewrites rows whose
        stored value disagrees with what ``canonical_name`` normalizes to now.
        """
        try:
            rows = self._conn.execute(
                "SELECT blackhole_id, canonical_name, normalized_name FROM entity_blackholes"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return 0
            raise
        repaired = 0
        for blackhole_id, canonical_name, stored in rows:
            fresh = normalize_entity_name(str(canonical_name or ""))
            if not fresh or fresh == str(stored or ""):
                continue
            self._conn.execute(
                "UPDATE entity_blackholes SET normalized_name=?, updated_at=datetime('now')"
                " WHERE blackhole_id=?",
                (fresh, blackhole_id),
            )
            repaired += 1
        return repaired

    def pending_rebuild_names(self) -> Set[str]:
        """Black holes whose derived-artifact rebuild has not finished (D4/I6).

        Name-string artifacts (briefs, digests, dossiers, top_topics, stats) that
        predate the flag must be withheld from non-owner callers while these are
        outstanding — withheld, never served stale.
        """
        try:
            rows = self._conn.execute(
                "SELECT normalized_name FROM entity_blackholes WHERE rebuild_state != 'complete'"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return set()
            raise
        return {str(r[0]) for r in rows if r[0]}

    def has_pending_rebuild(self) -> bool:
        return bool(self.pending_rebuild_names())

    # ------------------------------------------------------------- writes

    def blackhole_entity(
        self,
        *,
        entity_ref: str,
        processing_tier: str = "secure",
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Flag an entity off-limits. Additive — nothing is deleted or purged.

        `entity_ref` may be an entity_id or a name; a name with no entity behind
        it yet is accepted, so protection can be declared before the resolver
        ever mints the row.
        """
        if processing_tier not in PROCESSING_TIERS:
            raise ValueError(f"unknown processing_tier: {processing_tier}")
        ref = str(entity_ref or "").strip()
        if not ref:
            raise ValueError("entity_ref is required")

        entity_id, canonical_name, aliases_json = self._resolve_entity(ref)
        normalized = normalize_entity_name(canonical_name or ref)
        if not normalized:
            raise ValueError("entity_ref did not normalize to a usable name")

        existing = self.get(normalized)
        if existing:
            # Idempotent: already protected. Refresh the mutable bits, do not
            # restart a rebuild that may already have completed.
            with with_db_write():
                self._conn.execute(
                    """
                    UPDATE entity_blackholes
                    SET processing_tier=?, note=COALESCE(?, note), entity_id=?,
                        canonical_name=COALESCE(?, canonical_name),
                        aliases_json=?, updated_at=datetime('now')
                    WHERE normalized_name=?
                    """,
                    (
                        processing_tier,
                        note,
                        entity_id or existing["entity_id"],
                        canonical_name,
                        aliases_json,
                        normalized,
                    ),
                )
                commit_connection(self._conn)
            record = self.get(normalized) or {}
            return {**record, "already_blackholed": True, "notification_id": None}

        blackhole_id = _new_id("bh")
        with with_db_write():
            self._conn.execute(
                """
                INSERT INTO entity_blackholes
                    (blackhole_id, entity_id, normalized_name, canonical_name,
                     aliases_json, processing_tier, rebuild_state, note)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    blackhole_id,
                    entity_id,
                    normalized,
                    canonical_name,
                    aliases_json,
                    processing_tier,
                    note,
                ),
            )
            # D4: the notification is raised *before* the rebuild, so the owner
            # knows the hide is not yet complete across derived artifacts.
            notification_id = self._notify(
                blackhole_id=blackhole_id,
                entity_id=entity_id,
                normalized_name=normalized,
                kind="rebuild_needed",
                message=(
                    f"'{canonical_name or ref}' is now off-limits. A rebuild is needed before it "
                    "disappears from summaries, briefs and digests; until then those are withheld "
                    "from everyone but you."
                ),
            )
            commit_connection(self._conn)
        record = self.get(normalized) or {}
        return {**record, "already_blackholed": False, "notification_id": notification_id}

    def unblackhole_entity(self, *, entity_ref: str) -> Dict[str, Any]:
        """Lift the flag. Grants are NOT restored — normal permissions resume."""
        record = self.get(entity_ref)
        if record is None:
            return {"removed": False}
        with with_db_write():
            self._conn.execute(
                "DELETE FROM entity_blackholes WHERE blackhole_id=?", (record["blackhole_id"],)
            )
            self._resolve_notifications(record["blackhole_id"])
            notification_id = self._notify(
                blackhole_id=record["blackhole_id"],
                entity_id=record["entity_id"],
                normalized_name=record["normalized_name"],
                kind="reinclude_needed",
                message=(
                    f"'{record['canonical_name'] or record['normalized_name']}' is no longer "
                    "off-limits. A rebuild is needed before it reappears in summaries and digests."
                ),
            )
            commit_connection(self._conn)
        return {"removed": True, "blackhole_id": record["blackhole_id"], "notification_id": notification_id}

    def bind_entity_id(self, *, normalized_name: str, entity_id: str) -> bool:
        """Attach a freshly-minted entity_id to a name that was protected pre-emptively."""
        with with_db_write():
            cursor = self._conn.execute(
                "UPDATE entity_blackholes SET entity_id=?, updated_at=datetime('now') "
                "WHERE normalized_name=? AND entity_id=''",
                (str(entity_id), normalize_entity_name(normalized_name)),
            )
            commit_connection(self._conn)
        return bool(cursor.rowcount)

    # ------------------------------------------------------ rebuild state

    def _set_rebuild_state(self, entity_ref: str, state: str) -> bool:
        if state not in REBUILD_STATES:
            raise ValueError(f"unknown rebuild_state: {state}")
        record = self.get(entity_ref)
        if record is None:
            return False
        with with_db_write():
            self._conn.execute(
                "UPDATE entity_blackholes SET rebuild_state=?, updated_at=datetime('now') "
                "WHERE blackhole_id=?",
                (state, record["blackhole_id"]),
            )
            commit_connection(self._conn)
        return True

    def mark_rebuild_running(self, entity_ref: str) -> bool:
        return self._set_rebuild_state(entity_ref, "running")

    def mark_rebuild_complete(self, entity_ref: str) -> bool:
        record = self.get(entity_ref)
        if record is None:
            return False
        self._set_rebuild_state(entity_ref, "complete")
        with with_db_write():
            self._resolve_notifications(record["blackhole_id"], kinds=("rebuild_needed",))
            self._notify(
                blackhole_id=record["blackhole_id"],
                entity_id=record["entity_id"],
                normalized_name=record["normalized_name"],
                kind="rebuild_complete",
                message=(
                    f"'{record['canonical_name'] or record['normalized_name']}' is now fully hidden "
                    "everywhere outside your own view."
                ),
            )
            commit_connection(self._conn)
        return True

    def mark_rebuild_failed(self, entity_ref: str, *, reason: str = "") -> bool:
        record = self.get(entity_ref)
        if record is None:
            return False
        # The rebuild_needed notification stays open on purpose: the hide is
        # still incomplete, and the fail-closed withholding stays in force.
        self._set_rebuild_state(entity_ref, "failed")
        with with_db_write():
            self._notify(
                blackhole_id=record["blackhole_id"],
                entity_id=record["entity_id"],
                normalized_name=record["normalized_name"],
                kind="rebuild_failed",
                message=(
                    f"Rebuild failed for '{record['canonical_name'] or record['normalized_name']}'"
                    f"{(': ' + reason) if reason else ''}. It stays withheld from others until this "
                    "succeeds."
                ),
            )
            commit_connection(self._conn)
        return True

    # ------------------------------------------------------ notifications

    def _notify(
        self,
        *,
        blackhole_id: str,
        entity_id: str,
        normalized_name: str,
        kind: str,
        message: str,
    ) -> str:
        if kind not in NOTIFICATION_KINDS:
            raise ValueError(f"unknown notification kind: {kind}")
        notification_id = _new_id("bhn")
        self._conn.execute(
            """
            INSERT INTO blackhole_notifications
                (notification_id, blackhole_id, entity_id, normalized_name, kind, state, message)
            VALUES (?, ?, ?, ?, ?, 'open', ?)
            """,
            (notification_id, blackhole_id, entity_id, normalized_name, kind, message),
        )
        return notification_id

    def _resolve_notifications(
        self, blackhole_id: str, *, kinds: Optional[Sequence[str]] = None
    ) -> int:
        query = (
            "UPDATE blackhole_notifications SET state='resolved', resolved_at=datetime('now') "
            "WHERE blackhole_id=? AND state='open'"
        )
        params: List[Any] = [blackhole_id]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            query += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        return int(self._conn.execute(query, params).rowcount or 0)

    def notifications(self, *, state: Optional[str] = "open") -> List[Dict[str, Any]]:
        query = (
            "SELECT notification_id, blackhole_id, entity_id, normalized_name, kind, state,"
            " message, created_at, resolved_at FROM blackhole_notifications"
        )
        params: List[Any] = []
        if state:
            query += " WHERE state=?"
            params.append(state)
        query += " ORDER BY created_at DESC"
        try:
            rows = self._conn.execute(query, params).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return []
            raise
        return [
            {
                "notification_id": r[0],
                "blackhole_id": r[1],
                "entity_id": r[2],
                "normalized_name": r[3],
                "kind": r[4],
                "state": r[5],
                "message": r[6],
                "created_at": r[7],
                "resolved_at": r[8],
            }
            for r in rows
        ]

    def dismiss_notification(self, notification_id: str) -> bool:
        with with_db_write():
            cursor = self._conn.execute(
                "UPDATE blackhole_notifications SET state='resolved', resolved_at=datetime('now') "
                "WHERE notification_id=? AND state='open'",
                (notification_id,),
            )
            commit_connection(self._conn)
        return bool(cursor.rowcount)

    # ------------------------------------------------------------ helpers

    def _resolve_entity(self, ref: str) -> tuple:
        """(entity_id, canonical_name, aliases_json) — all defaulted when unminted."""
        try:
            row = self._conn.execute(
                "SELECT entity_id, canonical_name, aliases_json FROM entities WHERE entity_id=?",
                (ref,),
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT entity_id, canonical_name, aliases_json FROM entities"
                    " WHERE normalized_name=?",
                    (normalize_entity_name(ref),),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return ("", ref, "[]")
            raise
        if row is None:
            return ("", ref, "[]")
        return (str(row[0]), row[1], row[2] or "[]")


# ------------------------------------------------------- module-level reads
# Mirrors `exclusions.excluded_record_ids`: cheap helpers the read paths call
# without constructing a store.


def blackholed_entity_ids(conn: sqlite3.Connection) -> Set[str]:
    return BlackholeStore(conn).blackholed_entity_ids()


def blackholed_name_terms(conn: sqlite3.Connection) -> Set[str]:
    return BlackholeStore(conn).blackholed_name_terms()


def pending_rebuild_names(conn: sqlite3.Connection) -> Set[str]:
    return BlackholeStore(conn).pending_rebuild_names()


def secure_providers_for(conn: sqlite3.Connection, entity_ref: str) -> frozenset:
    """Providers allowed to process content mentioning this entity (D1).

    An entity that is not black-holed places no constraint, signalled by the
    empty frozenset — callers read that as "no restriction", not "nothing allowed".
    """
    tier = BlackholeStore(conn).processing_tier(entity_ref)
    if tier is None:
        return frozenset()
    return TIER_PROVIDERS[tier]
