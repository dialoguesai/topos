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

from .blackhole import EVERYONE, FULL, OWNER, WAITING_COLUMN, BlackholeStore, OffLimitsTerms, normalize_entity_name


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
        view: Optional[str] = None,
    ) -> None:
        self._conn = conn
        self._caller_class = caller_class if caller_class in CallerClass.ALL else CallerClass.UNKNOWN
        # Which entries this guard reads (`off_limits_view`). An entry the upgrade carried and the owner has not
        # acted on is withheld only where the answer can reach another person: the owner's own outside client
        # (OWNER_AGENT) reads as before the upgrade, and a grantee, a plugin and a caller the node cannot place
        # (UNKNOWN, the default) read every entry. A caller that built this guard with a class only to get the
        # full blocked set (the query pipeline does, as GRANTEE) says whose read it is with `view`.
        self._view = view if view is not None else self.view_of(self._caller_class)
        # D2: a routine may see protected entities only when it is local-only
        # end-to-end — engine-route retrieval *and* synthesis on the secure set.
        self._routine_local_only = bool(routine_local_only)
        self._ids: Optional[Set[str]] = None
        self._terms: Optional[OffLimitsTerms] = None
        self._pending: Optional[Set[str]] = None
        self._record_ids: Optional[Set[str]] = None

    @staticmethod
    def view_of(caller_class: str) -> str:
        """The view a caller class reads when nothing more is known of the caller."""
        from .off_limits_view import ROUTINE_LANE

        if caller_class in (CallerClass.OWNER_UI, CallerClass.OWNER_AGENT):
            return OWNER
        if caller_class == CallerClass.ROUTINE:
            return ROUTINE_LANE
        return EVERYONE

    @property
    def view(self) -> str:
        return self._view

    # ------------------------------------------------------------ posture

    @property
    def caller_class(self) -> str:
        return self._caller_class

    @property
    def sees_everything(self) -> bool:
        """True only for callers entitled to black-holed content."""
        if self._caller_class == CallerClass.OWNER_UI:
            return True
        # Processing locality is not permission to read owner-only data. A
        # routine requires a future verified owner-mode capability; this legacy
        # locality boolean cannot grant one.
        return False

    @property
    def active(self) -> bool:
        """Whether this guard actually filters anything (cheap early-out)."""
        if self.sees_everything:
            return False
        return bool(self._blocked_ids() or self._blocked_terms() or self.has_record_protections())

    def has_record_protections(self) -> bool:
        from .record_protection import RecordProtectionStore

        return not self.sees_everything and bool(RecordProtectionStore(self._conn).list())

    def active_apart_from_what_is_carried(self) -> bool:
        """`active`, not counting an entry that is carried and waiting (the store's FULL view): what a floor asks
        on the routine lane, where such an entry is applied to each item apart instead (`CarriedItems`). Every
        entry the owner made counts, and record protections count, as they always did."""
        if self.sees_everything:
            return False
        store = BlackholeStore(self._conn)
        return bool(store.blackholed_entity_ids(view=FULL) or store.terms(view=FULL) or self.has_record_protections())

    # -------------------------------------------------------------- lookups

    def _blocked_ids(self) -> Set[str]:
        if self._ids is None:
            self._ids = BlackholeStore(self._conn).blackholed_entity_ids(view=self._view)
        return self._ids

    def _blocked_terms(self) -> OffLimitsTerms:
        if self._terms is None:
            self._terms = BlackholeStore(self._conn).terms(view=self._view)
        return self._terms

    def _pending_names(self) -> Set[str]:
        if self._pending is None:
            self._pending = BlackholeStore(self._conn).pending_rebuild_names(view=self._view)
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
            from .record_protection import RecordProtectionStore

            protected_records = RecordProtectionStore(self._conn).blocked_ids()
            blocked = self._blocked_ids()
            if not blocked:
                self._record_ids = protected_records
            else:
                placeholders = ",".join("?" for _ in blocked)
                try:
                    rows = self._conn.execute(
                        f"SELECT DISTINCT record_id FROM entity_mentions "
                        f"WHERE entity_id IN ({placeholders})",
                        sorted(blocked),
                    ).fetchall()
                except sqlite3.OperationalError as exc:
                    # We have protected entity ids: losing their mention join
                    # cannot mean those entities have no canonical records.
                    # A name-only fallback misses nameless linked content.
                    raise sqlite3.OperationalError("protected record lineage is unavailable") from exc
                self._record_ids = protected_records | {str(r[0]) for r in rows if r and r[0]}
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
        record_id_keys: Sequence[str] = ("record_id", "message_id", "id", "event_id", "entry_id", "transaction_id", "segment_id", "contact_id"),
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

    def filter_observed_canonical_rows(self, rows, *, canonical_table):
        """Veto observed identity/contact/context on legacy canonical reads.

        The owner retains access. Unsupported or unavailable protection context
        withholds; this never confers a grant or certifies semantic absence.
        A fresh boundary is used for each call, not cached between DB snapshots.
        """
        if self.sees_everything:
            return list(rows)
        from ...permissions_v2.canonical import PolicyError
        from ...permissions_v2.entity_boundary import EntityBoundary
        from contextlib import closing, nullcontext
        from urllib.parse import quote

        try:
            # Check existing protection state as the old guard did; required
            # entity/record schemas are still checked in the fresh snapshot.
            # Do not trust request-local cached ids at egress.
            _ = self.active
            path = self._conn.execute("PRAGMA database_list").fetchone()[2]
            own_snapshot = bool(path) and not self._conn.in_transaction
            context = (closing(sqlite3.connect("file:" + quote(path, safe="/") + "?mode=ro", uri=True))
                       if own_snapshot else nullcontext(self._conn))
            with context as conn:
                if own_snapshot:
                    conn.execute("BEGIN")
                # The owner's own client reads these rows as before the upgrade: the boundary is built without
                # what is carried and waiting. Every other caller gets the boundary the share doors build.
                boundary = EntityBoundary(conn, waiting=self._view == EVERYONE)
                blocked = {str(row[0]) for row in conn.execute("SELECT record_id FROM owner_only_records")}
                result = []
                for row in rows:
                    try:
                        if any(str(row.get(key) or "") in blocked for key in
                               ("record_id", "message_id", "id", "event_id", "entry_id", "transaction_id", "segment_id", "contact_id")):
                            continue
                        if not boundary.legacy_veto(canonical_table, row):
                            result.append(row)
                    except (PolicyError, sqlite3.Error, TypeError, ValueError, RecursionError):
                        continue
                return result
        except (PolicyError, sqlite3.Error, TypeError, ValueError, RecursionError):
            return []

    # ------------------------------------------------- free-text egress scan

    def text_mentions_blackholed(self, text: Optional[str]) -> bool:
        """Substring scan for any protected name or alias.

        The belt to the id-join's suspenders: a mention the resolver never bound
        (a new nickname, a misspelling it did not fuzzy-match) carries no
        entity_id to filter on, so high-stakes egress — routine email, grantee
        answers — scans the rendered text as well.

        A name is looked for anywhere in the text, as it always was. A handle, a
        username or an id is looked for only as itself (`OffLimitsTerms`).
        """
        if self.sees_everything or not text:
            return False
        haystack = normalize_entity_name(str(text))
        if not haystack:
            return False
        return self._blocked_terms().found_in(haystack)

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
        # The legacy prose stores lack certified complete lineage. Suppress
        # their non-owner releases until a reader can recompute from permitted
        # inputs, regardless of whether the prose happens to repeat an id/name.
        return bool(self._pending_names()) or self.has_record_protections()

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


class CarriedItems:
    """What is carried and waiting, applied to ONE ITEM AT A TIME, the way the share doors match it (the fourth
    round, 7 Oct 2026). The routine lane's half of such an entry; see `off_limits_view`.

    Built on the share boundary itself, over nothing but the entries that are carried and waiting
    (`EntityBoundary(waiting=ONLY_WAITING)`). So it reaches what a share's boundary reaches from such an entry (the
    contact, its handles and usernames, the linked entity, its aliases and learned spellings, the records it is
    mentioned in) and it matches text with that module's own matcher: a name as the boundary reads names, a handle,
    a username or an id only as itself, nothing as a bare substring, nothing against a key
    (`EntityBoundary.item_names_protected`). There is no matcher in this class.

    It only ever removes. An item it cannot judge is withheld.

    It reads through one read transaction of its own, as the row filter above does
    (`filter_observed_canonical_rows`): the boundary, every row veto and every id it looks up see one state of
    the database, and the fresh-node rule (a message table this database never made holds no row) is only ever
    taken on the word of a catalog read inside a read. A caller that is itself inside a transaction, and a
    database with no file, are read through the caller's own connection. `close()` ends the read."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        from urllib.parse import quote

        from ...permissions_v2.entity_boundary import ONLY_WAITING, EntityBoundary

        self._own: Optional[sqlite3.Connection] = None
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        if path and not conn.in_transaction:
            self._own = sqlite3.connect("file:" + quote(path, safe="/") + "?mode=ro", uri=True)
            try:
                self._own.execute("BEGIN")
                self._boundary = EntityBoundary(self._own, waiting=ONLY_WAITING)
            except BaseException:
                self.close()
                raise
        else:
            self._boundary = EntityBoundary(conn, waiting=ONLY_WAITING)

    def close(self) -> None:
        """End this rule's own read. Safe to call twice; a rule that read through its caller's connection has
        nothing to end."""
        own, self._own = self._own, None
        if own is not None:
            try:
                own.close()
            except sqlite3.Error:
                pass

    def __del__(self) -> None:  # a rule that was dropped without being closed still ends its read
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def active(self) -> bool:
        return bool(self._boundary.active)

    @staticmethod
    def _table_of(item: Any) -> str:
        """The canonical table an item says it was built from, where it says."""
        if not isinstance(item, dict):
            return ""
        for key in ("canonical_table", "_table"):
            if isinstance(item.get(key), str) and item[key]:
                return item[key]
        source = item.get("retrieval_source")
        return source.partition("canonical:")[2] if isinstance(source, str) and source.startswith("canonical:") else ""

    def names(self, item: Any) -> bool:
        """Whether this item names a carried, waiting person: by an id it carries, or in its text as the share
        boundary reads text. An item built from a journal entry is read by the journal's rule, as at the doors."""
        if not self._boundary.active:
            return False
        try:
            from ...permissions_v2.entity_boundary import NAME_PART_TABLES

            return bool(self._boundary.item_names_protected(
                item, bare_parts_anywhere=self._table_of(item) in NAME_PART_TABLES))
        except Exception:  # noqa: BLE001 -- an item that cannot be judged is withheld
            return True

    def withhold_from(self, value: Any, *, text: bool = True) -> Any:
        """This answer with everything that names a carried, waiting person left out.

        An ITEM is an element of a list, wherever the list is: it goes as a whole when anything in it names the
        person (`names` reads every value at every depth, and every key for a name). Outside a list, a dictionary
        is the answer's own structure: each of its values is walked, a key that is the person's name is dropped
        with what is under it (an answer keyed by a person), and a text value that names the person is dropped
        with its key (a sentence the node composed from the owner's data sits there: "answer"). With `text=False`
        the texts of the structure itself are left as they are: for a caller whose structure holds nothing but
        its own vocabulary. Nothing is rewritten in place and nothing is added."""
        if not self._boundary.active:
            return value
        if isinstance(value, str):
            return "" if text and self.names(value) else value
        return self._walk(value, 0, text)

    #: How deep an answer's own structure may nest before what is left is judged as one item.
    _MAX_STRUCTURE = 16

    def _walk(self, value: Any, depth: int, text: bool) -> Any:
        if isinstance(value, (list, tuple)):
            return [element for element in value if not self.names(element)]
        if not isinstance(value, dict):
            return value
        kept: Dict[Any, Any] = {}
        for key, child in value.items():
            if self.names({key: None}):
                continue          # the key itself is the person's name: it goes with what is kept under it
            if isinstance(child, dict) and depth >= self._MAX_STRUCTURE:
                if not self.names(child):
                    kept[key] = child
            elif isinstance(child, str):
                if not (text and self.names(child)):
                    kept[key] = child
            else:
                kept[key] = self._walk(child, depth + 1, text)
        return kept

    def veto_rows(self, table: str, rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Canonical rows of `table`, without every row the share boundary vetoes (`legacy_veto`: the row's own
        text and ids, the records linked to it and, for a message, its conversation, roster and replies). A row
        that cannot be judged is left out."""
        if not self._boundary.active:
            return list(rows)
        kept: List[Dict[str, Any]] = []
        for row in rows:
            try:
                if not self._boundary.legacy_veto(table, row):
                    kept.append(row)
            except Exception:  # noqa: BLE001 -- unavailable context withholds
                continue
        return kept


def anything_is_carried(conn: sqlite3.Connection) -> bool:
    """Whether any entry of this database is carried and waiting as a whole. Read from the mark alone, so a
    database the upgrade step never wrote to (no table, or no such column) answers False without anything else
    being read. A database that cannot be read raises: it is never read as "nothing is carried"."""
    if WAITING_COLUMN not in {row[1] for row in conn.execute("PRAGMA table_info(entity_blackholes)")}:
        return False
    return any(record["carried_waiting"] for record in BlackholeStore(conn).list())


def carried_items_for_routine(conn: Optional[sqlite3.Connection], principal: Any = None, *,
                              current: bool = True) -> Optional[CarriedItems]:
    """The routine lane's rule for an entry that is carried and waiting, or None where it does not apply.

    None for every caller but a routine (a frame the control plane stamped `owner_automation`, whose stamp
    verified: `off_limits_view.is_routine_lane`), and for a routine while nothing is carried and waiting. For
    everyone it returns None for, every reader does exactly what it did before the fourth round.

    RAISES when something is carried and the boundary over it cannot be built. Every caller treats that as it
    treated any entry before: the floor stands, the answer is refused. It is never read as "nothing is carried"."""
    from .off_limits_view import is_routine_lane

    if conn is None or not is_routine_lane(principal, current=current):
        return None
    if not anything_is_carried(conn):
        return None
    carried = CarriedItems(conn)
    if not carried.active:
        from ...permissions_v2.canonical import PolicyError

        carried.close()
        raise PolicyError("entity_protection_lineage_unavailable")
    return carried


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
    from ...principal import OWNER_APP, current_principal

    principal = current_principal()
    # Only the channel verifier can confer the owner-mode exception. A caller
    # block, matching owner subject, raw tier, or local routine flag cannot.
    if getattr(principal, "cls", None) != OWNER_APP:
        # Still UNKNOWN: it filters as before. Which entries it reads is the request's own view: the owner's own
        # outside client (verified at the node's door) does not see what is carried and waiting; a caller the
        # node cannot place sees every entry.
        from .off_limits_view import for_request

        return BlackholeGuard(conn, caller_class=CallerClass.UNKNOWN, view=for_request(principal, current=False))
    caller = message.get("caller")
    if not isinstance(caller, dict):
        return owner_ui_guard(conn)
    return BlackholeGuard(
        conn,
        caller_class=CallerClass.resolve(
            mcp_source=caller.get("mcp_source"),
            is_grantee_request=bool(caller.get("is_grantee_request")),
            requester_is_owner=bool(caller.get("requester_is_owner", True)),
        ),
        routine_local_only=bool(caller.get("routine_local_only")),
    )
