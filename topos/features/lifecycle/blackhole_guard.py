"""The black-hole access predicate: who may see a protected entity, and how it hides.

Every read path in the engine asks this module the same two questions — *is this
caller the owner's own eyes?* and *does this row/text touch a black-holed
entity?* — so that the answer is decided in one place rather than re-derived at
each of the dozen-odd serving surfaces.

Two rules shape the whole design.

**Fail closed on identity.** `CallerClass.resolve()` maps anything it does not
positively recognise as the owner's own UI to a non-owner class. An unknown
caller is a caller that gets filtered. This is why the resolver takes
*server-derived* identity only: an MCP client can set `X-Topos-Client` to
whatever it likes, so that header may never be the thing that grants owner-UI
access (eval class C6.2 asserts exactly this).

**Hide by absence, never by denial (D5).** A black-holed entity must be
indistinguishable from one that never existed. So this module removes rows and
returns empty results; it does not raise, does not emit a "forbidden" outcome,
and does not offer a reason. A 403 that only appears for real entities is itself
a confirmation of existence — the leak the feature exists to prevent. Callers
that turn a `False` from here into an error message reintroduce the side
channel, which is why nothing in this API returns one.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .blackhole import BlackholeStore, normalize_entity_name


class CallerClass:
    """Server-derived caller identity. Only OWNER_UI sees black-holed entities."""

    OWNER_UI = "owner_ui"
    OWNER_AGENT = "owner_agent"
    ROUTINE = "routine"
    GRANTEE = "grantee"
    PLUGIN = "plugin"
    UNKNOWN = "unknown"

    ALL = (OWNER_UI, OWNER_AGENT, ROUTINE, GRANTEE, PLUGIN, UNKNOWN)

    # The owner's own first-party UI. Deliberately a single value: every way of
    # widening this set is a way of leaking.
    _OWNER_UI_SOURCES = frozenset({"topos_home_chat"})
    _ROUTINE_SOURCES = frozenset({"routine_executor"})
    _GRANTEE_SOURCES = frozenset({"plugin_attach", "rpt"})

    @classmethod
    def resolve(
        cls,
        *,
        mcp_source: Optional[str] = None,
        is_grantee_request: bool = False,
        requester_is_owner: bool = True,
    ) -> str:
        """Classify a caller from server-derived signals only.

        `mcp_source` must come from the gateway's own resolution of the
        transport/auth, never from a client-supplied header echoed back.
        """
        # A grantee is a grantee regardless of what it claims to be.
        if is_grantee_request or not requester_is_owner:
            return cls.GRANTEE
        source = str(mcp_source or "").strip().lower()
        if source in cls._GRANTEE_SOURCES:
            return cls.GRANTEE
        if source in cls._ROUTINE_SOURCES:
            return cls.ROUTINE
        if source in cls._OWNER_UI_SOURCES:
            return cls.OWNER_UI
        if source:
            return cls.OWNER_AGENT
        return cls.UNKNOWN


class BlackholeGuard:
    """Decides what a given caller may see of black-holed entities.

    Construct once per request and reuse: the id/name sets are read from SQLite
    on first use and cached for the life of the guard.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        caller_class: str = CallerClass.UNKNOWN,
        routine_local_only: bool = False,
    ) -> None:
        self._conn = conn
        self._caller_class = caller_class if caller_class in CallerClass.ALL else CallerClass.UNKNOWN
        # D2: a routine may see protected entities only when it is local-only
        # end-to-end — engine-route retrieval *and* synthesis on the secure set.
        self._routine_local_only = bool(routine_local_only)
        self._ids: Optional[Set[str]] = None
        self._terms: Optional[Set[str]] = None
        self._pending: Optional[Set[str]] = None
        self._record_ids: Optional[Set[str]] = None

    # ------------------------------------------------------------ posture

    @property
    def caller_class(self) -> str:
        return self._caller_class

    @property
    def sees_everything(self) -> bool:
        """True only for callers entitled to black-holed content."""
        if self._caller_class == CallerClass.OWNER_UI:
            return True
        if self._caller_class == CallerClass.ROUTINE and self._routine_local_only:
            return True
        return False

    @property
    def active(self) -> bool:
        """Whether this guard actually filters anything (cheap early-out)."""
        if self.sees_everything:
            return False
        return bool(self._blocked_ids() or self._blocked_terms())

    # -------------------------------------------------------------- lookups

    def _blocked_ids(self) -> Set[str]:
        if self._ids is None:
            self._ids = BlackholeStore(self._conn).blackholed_entity_ids()
        return self._ids

    def _blocked_terms(self) -> Set[str]:
        if self._terms is None:
            self._terms = BlackholeStore(self._conn).blackholed_name_terms()
        return self._terms

    def _pending_names(self) -> Set[str]:
        if self._pending is None:
            self._pending = BlackholeStore(self._conn).pending_rebuild_names()
        return self._pending

    def blocked_record_ids(self) -> Set[str]:
        """Canonical records that mention a protected entity, via `entity_mentions`.

        Canonical rows (messages, journal entries, calendar events) carry no
        entity id of their own — the link lives in the mention table. This is
        the exact half of the filter; a text scan is still needed alongside it
        for a name the resolver never bound.
        """
        if self.sees_everything:
            return set()
        if self._record_ids is None:
            blocked = self._blocked_ids()
            if not blocked:
                self._record_ids = set()
            else:
                placeholders = ",".join("?" for _ in blocked)
                try:
                    rows = self._conn.execute(
                        f"SELECT DISTINCT record_id FROM entity_mentions "
                        f"WHERE entity_id IN ({placeholders})",
                        sorted(blocked),
                    ).fetchall()
                except sqlite3.OperationalError as exc:
                    if "no such table" in str(exc).lower():
                        rows = []
                    else:
                        raise
                self._record_ids = {str(r[0]) for r in rows if r and r[0]}
        return self._record_ids

    def blocks_record_id(self, record_id: Optional[str]) -> bool:
        if self.sees_everything or not record_id:
            return False
        return str(record_id) in self.blocked_record_ids()

    def blocks_entity_id(self, entity_id: Optional[str]) -> bool:
        if self.sees_everything or not entity_id:
            return False
        return str(entity_id) in self._blocked_ids()

    def blocks_name(self, name: Optional[str]) -> bool:
        if self.sees_everything or not name:
            return False
        return normalize_entity_name(str(name)) in self._blocked_terms()

    # --------------------------------------------------------- SQL pushdown

    def sql_exclusion(self, column: str = "entity_id") -> tuple:
        """`(clause, params)` excluding protected entities, for a WHERE list.

        Pushed into SQL rather than applied to the result set because counts
        have to agree with rows. A `total` or a `type_counts` bucket computed
        before the filter still counts the hidden entity, and a count that
        moves when an entity is protected confirms the entity exists (D5). The
        only way to keep them consistent is to filter once, in the query.

        Returns `("", [])` when nothing is protected or the caller sees
        everything, so call sites can splice unconditionally.
        """
        if self.sees_everything:
            return ("", [])
        ids = sorted(self._blocked_ids())
        if not ids:
            return ("", [])
        placeholders = ",".join("?" for _ in ids)
        return (f"{column} NOT IN ({placeholders})", list(ids))

    # ----------------------------------------------------------- filtering

    def filter_entity_ids(self, entity_ids: Iterable[str]) -> List[str]:
        if self.sees_everything:
            return list(entity_ids)
        blocked = self._blocked_ids()
        return [e for e in entity_ids if str(e) not in blocked]

    def filter_rows(
        self,
        rows: Sequence[Dict[str, Any]],
        *,
        id_keys: Sequence[str] = ("entity_id",),
        name_keys: Sequence[str] = (),
    ) -> List[Dict[str, Any]]:
        """Drop rows touching a black-holed entity, by id and/or by name.

        `id_keys` covers both ends of a relation (e.g. subject *and* object of a
        fact) — a fact whose object is protected is keyed under some other
        subject, so filtering one end alone would miss it.
        """
        if self.sees_everything:
            return list(rows)
        kept: List[Dict[str, Any]] = []
        for row in rows:
            if any(self.blocks_entity_id(row.get(k)) for k in id_keys):
                continue
            if any(self.blocks_name(row.get(k)) for k in name_keys):
                continue
            kept.append(row)
        return kept

    def filter_canonical_rows(
        self,
        rows: Sequence[Dict[str, Any]],
        *,
        record_id_keys: Sequence[str] = ("record_id", "message_id", "id"),
        text_keys: Sequence[str] = ("content", "text", "body", "summary_text"),
    ) -> List[Dict[str, Any]]:
        """Drop canonical rows that mention a protected entity.

        Both halves are needed and neither is sufficient alone: the mention join
        catches records the resolver bound (exact, and survives paraphrase of the
        name), the text scan catches a spelling or nickname it never bound.
        """
        if self.sees_everything:
            return list(rows)
        kept: List[Dict[str, Any]] = []
        for row in rows:
            if any(self.blocks_record_id(row.get(k)) for k in record_id_keys):
                continue
            if any(self.text_mentions_blackholed(row.get(k)) for k in text_keys):
                continue
            kept.append(row)
        return kept

    # ------------------------------------------------- free-text egress scan

    def text_mentions_blackholed(self, text: Optional[str]) -> bool:
        """Substring scan for any protected name or alias.

        The belt to the id-join's suspenders: a mention the resolver never bound
        (a new nickname, a misspelling it did not fuzzy-match) carries no
        entity_id to filter on, so high-stakes egress — routine email, grantee
        answers — scans the rendered text as well.
        """
        if self.sees_everything or not text:
            return False
        haystack = normalize_entity_name(str(text))
        if not haystack:
            return False
        return any(term and term in haystack for term in self._blocked_terms())

    def withhold_if_mentions(self, text: Optional[str]) -> Optional[str]:
        """Return the text, or None when it touches a protected entity.

        Whole-artifact withholding rather than redaction-in-place: D3 is full
        exclusion, and a partially-scrubbed summary still leaks by shape (the
        gap where a name was is itself information).
        """
        return None if self.text_mentions_blackholed(text) else text

    # ------------------------------------- D4/I6: the pending-rebuild window

    def withhold_pending_rebuild(self) -> bool:
        """True when name-string artifacts must be withheld from this caller.

        Between flipping the flag and the rebuild landing, artifacts like briefs
        and digests still carry the name baked into prose. Serving the stale one
        would leak; so during the window they are withheld outright. Fail closed,
        never stale.
        """
        if self.sees_everything:
            return False
        return bool(self._pending_names())

    def filter_name_string_artifacts(
        self, artifacts: Sequence[Dict[str, Any]], *, text_keys: Sequence[str]
    ) -> List[Dict[str, Any]]:
        """Filter prose artifacts (briefs, digests, dossiers) by text scan.

        While a rebuild is pending, every artifact of this kind is withheld —
        not just the ones that happen to mention the entity today — because a
        pre-flag artifact may name it in a form the scan does not catch.
        """
        if self.sees_everything:
            return list(artifacts)
        if self.withhold_pending_rebuild():
            return []
        kept: List[Dict[str, Any]] = []
        for artifact in artifacts:
            if any(self.text_mentions_blackholed(artifact.get(k)) for k in text_keys):
                continue
            kept.append(artifact)
        return kept


def guard_for(
    conn: sqlite3.Connection,
    *,
    mcp_source: Optional[str] = None,
    is_grantee_request: bool = False,
    requester_is_owner: bool = True,
    routine_local_only: bool = False,
) -> BlackholeGuard:
    """Build a guard from server-derived request identity."""
    return BlackholeGuard(
        conn,
        caller_class=CallerClass.resolve(
            mcp_source=mcp_source,
            is_grantee_request=is_grantee_request,
            requester_is_owner=requester_is_owner,
        ),
        routine_local_only=routine_local_only,
    )


def owner_ui_guard(conn: sqlite3.Connection) -> BlackholeGuard:
    """Guard for the owner's own authenticated UI session.

    Reserved for surfaces where owner-UI identity is established by the
    transport itself — the local API-key-authenticated HTTP routes. Do not
    reach for this to "make a test pass" on a path whose caller is not
    transport-proven; that is how the owner-only surface becomes everyone's.
    """
    return BlackholeGuard(conn, caller_class=CallerClass.OWNER_UI)


def guard_from_message(conn: sqlite3.Connection, message: Dict[str, Any]) -> BlackholeGuard:
    """Guard for a control-plane-forwarded WebSocket request.

    The control plane stamps a top-level `caller` block from identity *it*
    resolved during auth (never from a client-supplied header). It sits outside
    `payload` deliberately: `payload` carries client-controlled arguments, and a
    caller that could name its own class could name itself the owner.

    A message with no `caller` block resolves to UNKNOWN, which filters. That is
    the fail-closed choice for the version skew where the engine ships ahead of
    the control plane: with no black holes configured the guard is inert and
    nothing changes, and with black holes configured the owner briefly loses
    sight of a protected entity in a proxied view rather than that entity
    leaking to every third-party agent. A degraded view is recoverable; a leak
    is not.
    """
    caller = message.get("caller")
    if not isinstance(caller, dict):
        return BlackholeGuard(conn, caller_class=CallerClass.UNKNOWN)
    return BlackholeGuard(
        conn,
        caller_class=CallerClass.resolve(
            mcp_source=caller.get("mcp_source"),
            is_grantee_request=bool(caller.get("is_grantee_request")),
            requester_is_owner=bool(caller.get("requester_is_owner", True)),
        ),
        routine_local_only=bool(caller.get("routine_local_only")),
    )
