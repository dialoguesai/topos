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

import functools
import json
import re
import sqlite3
import uuid
from typing import Any, Dict, List, Optional, Sequence, Set

from ...storage.db.write_gate import commit_connection, with_db_write

PROCESSING_TIERS = ("secure", "local_only")


def stricter_tier(one: str, other: str) -> str:
    """The stricter of two processing tiers (PROCESSING_TIERS runs from the wider to the stricter)."""
    return max(one, other, key=PROCESSING_TIERS.index)


def start_waiting_clean_up(store: "BlackholeStore", entity_ref: str, *, processing_tier: str,
                           note: Optional[str]) -> Optional[Dict[str, Any]]:
    """The owner marking an entry that is already off-limits and whose clean-up has not completed: what the mark
    does to the entry itself, or None when this is not that case (review R1 node, R-B1).

    Such a mark starts the clean-up (the caller runs it). It never loosens the entry: the app and the control
    plane send a tier with every mark, the default one when the owner chose none, and `blackhole_entity` would
    write that over a stricter tier. A mark may still tighten the tier or set a note. When it would change
    nothing, the entry is not rewritten at all."""
    waiting = store.get(entity_ref)
    if waiting is not None and has_waiting(waiting):
        # The tier is checked BEFORE the entry is made full (the fourth round, the third round's own B9): a mark
        # the node refuses must leave a waiting entry waiting. It used to be made full first, and then stayed
        # full with its clean-up never run until the next mark that was valid.
        if processing_tier not in PROCESSING_TIERS:
            raise ValueError(f"unknown processing_tier: {processing_tier}")
        # The owner's act on an entry the upgrade carried (ruling P.3): from here it is an ordinary entry.
        store.make_full(waiting["blackhole_id"])
        waiting = store.get(entity_ref)
    if waiting is None or waiting["rebuild_state"] == "complete":
        return None
    if processing_tier not in PROCESSING_TIERS:
        raise ValueError(f"unknown processing_tier: {processing_tier}")
    tier = stricter_tier(processing_tier, waiting["processing_tier"])
    if tier == waiting["processing_tier"] and note is None:
        return {**waiting, "already_blackholed": True, "notification_id": None}
    return store.blackhole_entity(entity_ref=entity_ref, processing_tier=tier, note=note)
REBUILD_STATES = ("pending", "running", "complete", "failed")
NOTIFICATION_KINDS = (
    "rebuild_needed",
    "rebuild_complete",
    "rebuild_failed",
    "reinclude_needed",
    # The upgrade step's own two (contact_excludes): what it carried, and that it could not finish.
    "carried_over",
    "carry_failed",
)

# D1: Red Pill's TEE counts as secure. These are the only providers that may
# ever receive content mentioning a black-holed entity. Anything absent from the
# tier's set is refused — the gate fails closed rather than falling back.
TIER_PROVIDERS: Dict[str, frozenset] = {
    "secure": frozenset({"ollama", "huggingface", "redpill"}),
    "local_only": frozenset({"ollama", "huggingface"}),
}


#: The aliases of an entry that are a handle, a username or an id, not a name (review R1 node, R-M5). They stay
#: among ``aliases_json`` too, so every reader that matches aliases keeps matching them; this list is what tells
#: the share boundary to read them as it reads a contact's handles (whole, `entity_boundary._handle_keys`) and
#: never as names: an id's or an address's words are not parts of anybody's name. Normalized like the aliases. A
#: nullable column added in place by the first write that needs it (``ensure_identifier_aliases``), with no
#: migration number: every reader names the columns it reads, and a row without it reads every alias as a name.
IDENTIFIERS_COLUMN = "identifier_aliases_json"


#: Which entries one reader of this list sees (the third fix round, ruling P, 7 Oct 2026).
#:
#: The upgrade that carries the older per-person "exclude" choices here runs unasked, and an exclude was a choice
#: about SHARING. So an entry it makes is CARRIED AND WAITING until the owner acts on it: every reader whose answer
#: can leave the node toward another person sees it (EVERYONE), and every reader that serves the owner himself does
#: not (OWNER): his own outside client, the models his node calls for him and the producers of his own summaries
#: behave exactly as before the upgrade. EVERYONE is every caller's default, so a reader nobody classified keeps
#: reading every entry, as it did before there were two views.
EVERYONE = "everyone"
OWNER = "owner"
#: The fourth round (7 Oct 2026). What a WHOLE-LIST rule reads on the routine lane: the floors that answer "one
#: entry, so nothing" and the name scan that finds a name inside any word. Every entry but one that is carried and
#: waiting as a whole; an entry the owner made is read whole here, with whatever the upgrade added to it. What this
#: view leaves out is not released on that lane: it is applied to every item apart, the way the share doors match
#: it (`blackhole_guard.CarriedItems`). No reader takes this view without that other half.
FULL = "full"
VIEWS = (EVERYONE, OWNER, FULL)

#: What of an entry is carried and waiting: ``{"whole": bool, "terms": [normalized alias, ...], "entity_id": str}``,
#: or NULL for an entry with nothing waiting. ``whole`` is the entry itself (made by the upgrade step, the owner has
#: not acted); ``terms`` on a full entry are the names and identifiers the step added to an entry the owner had
#: already made, and ``entity_id`` is the entity the step linked such an entry to when it had none.
#: On the row itself, so it lives exactly as long as the entry does: a restart, a second run of the step, a backup
#: and its restore all keep it, and removing the entry removes it. A nullable column added in place by the first
#: write that needs it, like IDENTIFIERS_COLUMN. A value that cannot be read marks nothing: the entry is then a
#: full entry for every reader, which is the direction that protects.
WAITING_COLUMN = "carried_waiting_json"

#: An entry's own id (``_new_id("bh")``). The owner's doors accept it where they take an entity id or a name, so an
#: entry with no linked entity can be acted on and removed; text of this shape is never made into a new entry.
ENTRY_ID = re.compile(r"^bh_[0-9a-f]{12}$")
NO_SUCH_ENTRY = "no such off-limits entry"


def starts_like_an_entry_id(text: Any) -> bool:
    """Whether this text starts as an entry's own id does, in any letter case. No NEW entry is ever made under
    such text (the second re-check, R3-L2): a client that sends an id that is nearly right must be told there is no
    such entry, not be given a new one named by the id."""
    return str(text or "").strip()[:3].lower() == "bh_"

#: The step's one notice (``BlackholeStore.note_carried_over``): its kind, and what stands in the notification's
#: ``blackhole_id`` column, which no entry's id can equal.
CARRIED_OVER = "carried_over"
CARRY_NOTICE_ID = "carry-contact-excludes-to-off-limits"


def _columns(conn: sqlite3.Connection) -> Set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(entity_blackholes)")}


def has_identifier_aliases(conn: sqlite3.Connection) -> bool:
    return IDENTIFIERS_COLUMN in _columns(conn)


def ensure_waiting_column(conn: sqlite3.Connection) -> None:
    """Add ``WAITING_COLUMN`` where it is missing. Found by PRAGMA, so a present column never reaches ALTER."""
    if WAITING_COLUMN in _columns(conn):
        return
    with with_db_write():
        conn.execute(f"ALTER TABLE entity_blackholes ADD COLUMN {WAITING_COLUMN} TEXT")
        commit_connection(conn)


def _waiting(raw: Any) -> tuple:
    """``(whole, terms, entity id)`` of one stored WAITING_COLUMN value; ``(False, [], "")`` for none or one that
    cannot be read."""
    if not raw:
        return False, [], ""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return False, [], ""
    if not isinstance(value, dict) or not isinstance(value.get("terms", []), list):
        return False, [], ""
    terms = [str(term) for term in value.get("terms", []) if isinstance(term, str) and term]
    linked = value.get("entity_id")
    return value.get("whole") is True, terms, linked if isinstance(linked, str) else ""


def ensure_identifier_aliases(conn: sqlite3.Connection) -> None:
    """Add ``IDENTIFIERS_COLUMN`` where it is missing. Found by PRAGMA, so a present column never reaches ALTER."""
    if has_identifier_aliases(conn):
        return
    with with_db_write():
        conn.execute(f"ALTER TABLE entity_blackholes ADD COLUMN {IDENTIFIERS_COLUMN} TEXT")
        commit_connection(conn)


def _purge_message_search(conn: sqlite3.Connection) -> None:
    """A black hole moves the protection revision: every p2c search index for this
    database is deleted now, not at its next rebuild. Never raises."""
    from ...permissions_v2.search_index import purge_for_database
    purge_for_database(conn)


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


def has_waiting(record: Dict[str, Any]) -> bool:
    """Whether anything of this entry is carried and waiting: the entry itself, names the upgrade added to it, or
    the entity the upgrade linked it to."""
    return bool(record.get("carried_waiting") or record.get("carried_waiting_aliases")
                or record.get("carried_waiting_entity_id"))


def _view(view: str) -> str:
    """A view by name. Anything else is a mistake in the caller, and is never read as the narrower view."""
    if view not in VIEWS:
        raise ValueError(f"unknown off-limits view: {view!r}")
    return view


def _in_a_run(term: str) -> bool:
    """Whether every letter of this term is of a script written with no space between words, and there are at
    least two: the share boundary's own list and floor (``entity_boundary.UNSPACED``), which is the clean-up's."""
    from ...permissions_v2.entity_boundary import UNSPACED, UNSPACED_TERM_CHARS

    letters = [ch for ch in term if ch.isalnum()]
    return len(letters) >= UNSPACED_TERM_CHARS and all(UNSPACED.match(ch) for ch in letters)


@functools.lru_cache(maxsize=256)
def _identifier_pattern(identifiers: frozenset) -> Optional["re.Pattern[str]"]:
    """One pattern for the identifiers that are matched only as themselves: each stands where no letter or digit
    touches either end of it, its own words apart by whitespace as written."""
    if not identifiers:
        return None
    body = "|".join(r"\s+".join(re.escape(word) for word in term.split())
                    for term in sorted(identifiers, key=lambda term: (-len(term), term)))
    return re.compile(rf"(?<![^\W_])(?:{body})(?![^\W_])")


#: A name of this many letters or more is also found with an "s" glued on: a plural, or a possessive written with no
#: apostrophe ("sams" for "Sam's"; with the apostrophe the normalisation already took the "'s" off). Shorter names
#: are not, so "ed" is never found in "eds", nor "j" in "js".
NAME_PLURAL_MIN_CHARS = 3


@functools.lru_cache(maxsize=256)
def _name_pattern(names: frozenset) -> Optional["re.Pattern[str]"]:
    """One pattern for the names that are matched as whole words (BL-112, the owner's ruling of 8 Oct 2026): each
    stands where no letter or digit touches either end of it, with an "s" after it allowed from
    ``NAME_PLURAL_MIN_CHARS`` letters, its own words apart by whitespace as written. "sam" is found in "sam", "sam's"
    (normalised to "sam") and "sams", never in "same", "samples" or "balsam"; "al" never in "also" or "retrieval"."""
    if not names:
        return None
    ordered = sorted(names, key=lambda term: (-len(term), term))
    body = "|".join(r"\s+".join(re.escape(word) for word in term.split()) + ("s?" if len(term) >= NAME_PLURAL_MIN_CHARS
                                                                            else "")
                    for term in ordered)
    return re.compile(rf"(?<![^\W_])(?:{body})(?![^\W_])")


class OffLimitsTerms:
    """The names and the identifiers of the Off-limits entries one reader sees, and how each is looked for in text
    that was normalised with ``normalize_entity_name``.

    A NAME of an entry the owner made, or of a carried entry he has made fully Off-limits, is looked for as whole
    words (BL-112, the owner's ruling of 8 Oct 2026, ``_name_pattern``): until then it was found anywhere in the text,
    inside a longer word too, and a short name hid a great deal ("Ed" dropped 42% of the outside client's query
    results). It is looked for in the text and in ``values`` both, so a name that a serialisation glued to an escape
    ("\\nSam") is still found. A name that is carried and WAITING (``loose``: the whole entry, or a name the upgrade
    added to a full one) is looked for as before, anywhere in the text; so is one written in a script with no spaces.
    A caller that names no ``loose`` set gets every name read that way, as before.

    An IDENTIFIER (a handle, a username, a contact id, an address, a number: ``IDENTIFIERS_COLUMN``) matches only as
    itself, by the owner's decision of 7 Oct 2026: one with a digit or an ``@`` keeps the same reading as a name
    (it is long and particular enough to stand anywhere), and any other must stand as a whole token, so the
    username "al" is not found in "also" and the handle "work" not in "network". An identifier is never looked for
    in the KEYS of a structured value, only in its values: a caller that scans a serialised object passes the
    values apart (``values``). A term that is some entry's name is a name, whoever else lists it as an identifier.

    One written wholly in a script with no space between words (Han, kana, Thai, Lao, Khmer) is found anywhere
    too, from two characters (the fourth round). "A whole token" is "no letter touches either end", and in those
    scripts a letter always does: such a handle stopped being found in any sentence that held it when identifiers
    were narrowed, where until then every alias was a plain substring. The clean-up and the share boundary read
    such a term the same way (``_in_a_run``).
    """

    __slots__ = ("names", "identifiers", "_loose", "_named", "_anywhere", "_whole")

    def __init__(self, names: Set[str] = frozenset(), identifiers: Set[str] = frozenset(), *,
                 loose: Optional[Set[str]] = None) -> None:
        self.names = frozenset(term for term in names if term)
        self.identifiers = frozenset(term for term in identifiers if term) - self.names
        # The names found anywhere, inside a longer word too: carried and waiting, or written with no spaces.
        self._loose = (self.names if loose is None else frozenset(term for term in loose if term) & self.names) | \
            frozenset(term for term in self.names if _in_a_run(term))
        self._named = _name_pattern(self.names - self._loose)
        self._anywhere = frozenset(term for term in self.identifiers
                                   if "@" in term or any(ch.isdigit() for ch in term) or _in_a_run(term))
        self._whole = _identifier_pattern(self.identifiers - self._anywhere)

    def __bool__(self) -> bool:
        return bool(self.names or self.identifiers)

    def __iter__(self):
        """Every term, for a caller that compares a whole normalised name with the set."""
        return iter(self.names | self.identifiers)

    def __contains__(self, term: object) -> bool:
        return term in self.names or term in self.identifiers

    def __len__(self) -> int:
        return len(self.names) + len(self.identifiers)

    def found(self, text: Optional[str], *, values: Optional[str] = None) -> Optional[str]:
        """The first term found in this normalised text, or None. ``values`` is the same content without the keys
        of any structure it was serialised from; where a caller has no keys to leave out it is the text itself."""
        if not text and not values:
            return None
        text = text or ""
        for term in self._loose:
            if term in text:
                return term
        if self._named is not None:
            for named in (text,) if values is None or values == text else (text, values):
                hit = self._named.search(named)
                if hit is not None:
                    return hit.group(0)
        scanned = text if values is None else values
        if not scanned:
            return None
        for term in self._anywhere:
            if term in scanned:
                return term
        if self._whole is not None:
            hit = self._whole.search(scanned)
            if hit is not None:
                return hit.group(0)
        return None

    def found_in(self, text: Optional[str], *, values: Optional[str] = None) -> bool:
        return self.found(text, values=values) is not None

    @property
    def loose(self) -> frozenset:
        """The names looked for anywhere in the text (carried and waiting, or written with no spaces)."""
        return self._loose


def terms_of(record: Dict[str, Any]) -> OffLimitsTerms:
    """The names and identifiers of ONE entry as the store returns it (``BlackholeStore.list``): its stored name, a
    fresh normalisation of its canonical name (the two can disagree, see ``blackholed_name_terms``) and its
    aliases, less the ones it lists as identifiers."""
    identifiers = set(record.get("identifier_aliases") or [])
    names = {str(record.get("normalized_name") or ""), *(record.get("aliases") or [])}
    fresh = normalize_entity_name(str(record.get("canonical_name") or ""))
    if fresh and not (record.get("normalized_name") in identifiers):
        names.add(fresh)
    names -= identifiers | {""}
    # Carried and waiting (BL-112 left them as they were): the whole entry, or the names the upgrade added to one
    # the owner made. Only the entries the owner made, and carried ones he made fully Off-limits, read whole words.
    loose = names if record.get("carried_waiting") else names & set(record.get("carried_waiting_aliases") or [])
    return OffLimitsTerms(names, identifiers, loose=loose)


class BlackholeStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _legacy_table_missing(self, exc: sqlite3.OperationalError) -> bool:
        if not _missing_table(exc):
            return False
        try:
            migrated = self._conn.execute("SELECT 1 FROM wiki_schema_migrations WHERE migration_id='entity_blackhole_v1'").fetchone()
        except sqlite3.OperationalError as ledger_exc:
            if "no such table: wiki_schema_migrations" not in str(ledger_exc).lower():
                raise
            migrated = None
        if migrated:
            raise sqlite3.OperationalError("entity protection schema is unavailable") from exc
        return True

    # -------------------------------------------------------------- reads

    def is_blackholed(self, entity_ref: str, *, view: str = EVERYONE) -> bool:
        """True if this entity_id or name is black-holed. Fails closed on a sick DB."""
        ref = str(entity_ref or "").strip()
        if not ref:
            return False
        if self._owner_view_differs(view):
            return self._is_blackholed_for_owner(ref, whole_only=view == FULL)
        try:
            row = self._conn.execute(
                "SELECT 1 FROM entity_blackholes WHERE entity_id=? OR normalized_name=?",
                (ref, normalize_entity_name(ref)),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if self._legacy_table_missing(exc):
                return False
            raise
        return row is not None

    def _owner_view_differs(self, view: str) -> bool:
        """Whether a view that leaves out what is carried (OWNER, FULL) can differ from EVERYONE on this database:
        only while some entry here carries a mark. On every other database, the ones the upgrade step never wrote
        to included, such a view is the very same read EVERYONE makes, statement for statement. (Until the fifth
        round the test was the waiting column itself, which only the step's first write added. Since migration 81
        every database has the column, so the question is asked of the rows.)"""
        if _view(view) == EVERYONE or WAITING_COLUMN not in _columns(self._conn):
            return False
        return self._conn.execute(
            f"SELECT 1 FROM entity_blackholes WHERE {WAITING_COLUMN} IS NOT NULL LIMIT 1").fetchone() is not None

    def _is_blackholed_for_owner(self, ref: str, *, whole_only: bool = False) -> bool:
        """`is_blackholed` in the OWNER view: an entry that is carried and waiting does not count, nor does a full
        entry reached only through the entity the upgrade linked it to. With `whole_only` (the FULL view) only the
        first is left out: an entry the owner made counts however it is reached."""
        normalized = normalize_entity_name(ref)
        rows = self._conn.execute(
            f"SELECT entity_id, normalized_name, {WAITING_COLUMN} FROM entity_blackholes "
            "WHERE entity_id=? OR normalized_name=?", (ref, normalized)).fetchall()
        for entity_id, name, raw in rows:
            whole, _terms, linked = _waiting(raw)
            if whole:
                continue
            if not whole_only and name != normalized and linked and linked == entity_id == ref:
                continue
            return True
        return False

    def _record_columns(self) -> str:
        """The columns of one record; the identifier list and the waiting mark read NULL where this database has no
        such column yet."""
        present = _columns(self._conn)
        identifiers = IDENTIFIERS_COLUMN if IDENTIFIERS_COLUMN in present else "NULL"
        waiting = WAITING_COLUMN if WAITING_COLUMN in present else "NULL"
        return ("blackhole_id, entity_id, normalized_name, canonical_name, aliases_json, processing_tier, "
                f"rebuild_state, note, created_at, updated_at, {identifiers}, {waiting}")

    def get(self, entity_ref: str, *, view: str = EVERYONE) -> Optional[Dict[str, Any]]:
        """One entry, by its entity id, by a name, or by its own id (ENTRY_ID). In the OWNER view an entry that is
        carried and waiting is not there, and a full entry is returned without its waiting names."""
        ref = str(entity_ref or "").strip()
        if not ref:
            return None
        try:
            row = self._conn.execute(
                f"SELECT {self._record_columns()} FROM entity_blackholes "
                "WHERE blackhole_id=? OR entity_id=? OR normalized_name=?",
                (ref, ref, normalize_entity_name(ref)),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if self._legacy_table_missing(exc):
                return None
            raise
        found = self._viewed([self._row_to_dict(row)] if row else [], view)
        return found[0] if found else None

    def list(self, *, view: str = EVERYONE) -> List[Dict[str, Any]]:
        try:
            rows = self._conn.execute(
                f"SELECT {self._record_columns()} FROM entity_blackholes ORDER BY created_at DESC"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if self._legacy_table_missing(exc):
                return []
            raise
        return self._viewed([self._row_to_dict(r) for r in rows], view)

    @staticmethod
    def _viewed(records: List[Dict[str, Any]], view: str) -> List[Dict[str, Any]]:
        """These records as one view sees them (EVERYONE: as stored). The OWNER view leaves out every entry that is
        carried and waiting, and takes the waiting names and identifiers off a full entry. The FULL view leaves
        out the same entries and takes nothing off the others."""
        if _view(view) == EVERYONE:
            return records
        seen: List[Dict[str, Any]] = []
        for record in records:
            if record["carried_waiting"]:
                continue
            waiting = set(record["carried_waiting_aliases"]) if view == OWNER else set()
            if waiting or (view == OWNER and record["carried_waiting_entity_id"]):
                record = {**record,
                          "entity_id": "" if record["carried_waiting_entity_id"] else record["entity_id"],
                          "aliases": [alias for alias in record["aliases"] if alias not in waiting],
                          "identifier_aliases": [alias for alias in record["identifier_aliases"]
                                                 if alias not in waiting]}
            seen.append(record)
        return seen

    def terms(self, *, view: str = EVERYONE) -> OffLimitsTerms:
        """Every name and identifier this view sees, for a reader that scans text (``OffLimitsTerms``)."""
        names: Set[str] = set()
        identifiers: Set[str] = set()
        loose: Set[str] = set()
        for record in self.list(view=view):
            one = terms_of(record)
            names |= one.names
            identifiers |= one.identifiers
            loose |= one.loose
        return OffLimitsTerms(names, identifiers, loose=loose)

    @staticmethod
    def _row_to_dict(row: Sequence[Any]) -> Dict[str, Any]:
        whole, waiting, waiting_entity = _waiting(row[11]) if len(row) > 11 else (False, [], "")
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
            # Which of the aliases are a handle, a username or an id (IDENTIFIERS_COLUMN); empty for an entry
            # made before the column, whose aliases all read as names.
            "identifier_aliases": _normalized_aliases(row[10]) if len(row) > 10 else [],
            # Carried by the upgrade and the owner has not acted (WAITING_COLUMN): the whole entry, or on a full
            # entry the names and identifiers the upgrade added. Never shared; nothing else changed for them.
            "carried_waiting": whole,
            "carried_waiting_aliases": waiting,
            # The entity the upgrade linked a full entry to (empty when it linked none): waiting like the names.
            "carried_waiting_entity_id": waiting_entity if waiting_entity and waiting_entity == row[1] else "",
        }

    def processing_tier(self, entity_ref: str, *, view: str = EVERYONE) -> Optional[str]:
        record = self.get(entity_ref, view=view)
        return record["processing_tier"] if record else None

    # ---------------------------------------------------- hot-path lookups

    def blackholed_entity_ids(self, *, view: str = EVERYONE) -> Set[str]:
        """Entity ids to exclude from every non-owner read. Empty ids are dropped."""
        if self._owner_view_differs(view):
            return {str(record["entity_id"]) for record in self.list(view=view) if record["entity_id"]}
        try:
            rows = self._conn.execute(
                "SELECT entity_id FROM entity_blackholes WHERE entity_id != ''"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if self._legacy_table_missing(exc):
                return set()
            raise
        return {str(r[0]) for r in rows if r[0]}

    def blackholed_name_terms(self, *, view: str = EVERYONE) -> Set[str]:
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
        if self._owner_view_differs(view):
            owner_terms: Set[str] = set()
            for record in self.list(view=view):
                owner_terms.update(filter(None, (str(record["normalized_name"] or ""),
                                                 normalize_entity_name(str(record["canonical_name"] or "")))))
                owner_terms.update(record["aliases"])
            return owner_terms
        try:
            rows = self._conn.execute(
                "SELECT normalized_name, aliases_json, canonical_name FROM entity_blackholes"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if self._legacy_table_missing(exc):
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
            if self._legacy_table_missing(exc):
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

    def pending_rebuild_names(self, *, view: str = EVERYONE) -> Set[str]:
        """Black holes whose derived-artifact rebuild has not finished (D4/I6).

        Name-string artifacts (briefs, digests, dossiers, top_topics, stats) that
        predate the flag must be withheld from non-owner callers while these are
        outstanding — withheld, never served stale.
        """
        if self._owner_view_differs(view):
            return {str(record["normalized_name"]) for record in self.list(view=view)
                    if record["rebuild_state"] != "complete" and record["normalized_name"]}
        try:
            rows = self._conn.execute(
                "SELECT normalized_name FROM entity_blackholes WHERE rebuild_state != 'complete'"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if self._legacy_table_missing(exc):
                return set()
            raise
        return {str(r[0]) for r in rows if r[0]}

    def has_pending_rebuild(self, *, view: str = EVERYONE) -> bool:
        return bool(self.pending_rebuild_names(view=view))

    # ------------------------------------------------------------- writes

    def blackhole_entity(
        self,
        *,
        entity_ref: str,
        processing_tier: str = "secure",
        note: Optional[str] = None,
        aliases: Sequence[str] = (),
        identifiers: Sequence[str] = (),
        notice: Optional[str] = None,
        carried: bool = False,
    ) -> Dict[str, Any]:
        """Flag an entity off-limits. Additive — nothing is deleted or purged.

        `entity_ref` may be an entity_id or a name; a name with no entity behind
        it yet is accepted, so protection can be declared before the resolver
        ever mints the row. `aliases` are further NAMES the flag also matches
        (normalized like the stored ones), added to whatever the entity carries
        and never removing any. `identifiers` are a contact's handles, usernames
        and id: the flag matches them too, each only as itself (IDENTIFIERS_COLUMN);
        one that is also a name of the entry stays a name. A new entry's own name
        is an identifier only when the caller lists `entity_ref` among them and
        not among `aliases` (a contact with no usable name, carried under a
        handle). `notice` replaces the words of the notification a new entry
        raises.

        `carried` is the upgrade step's (contact_excludes): the new entry is
        CARRIED AND WAITING (WAITING_COLUMN, the views above) and raises no
        notice of its own. Every other flag is the owner's act: on an entry
        that has anything waiting it makes the entry full (`make_full`).
        `entity_ref` may also be an entry's own id (ENTRY_ID): the flag then
        acts on that entry and no other, and text of that shape that is no
        entry raises LookupError; no entry is ever made under it, nor under
        any other text that starts as an id does (`starts_like_an_entry_id`):
        such text acts on an entry that is already there under that very
        name, and otherwise raises LookupError.
        """
        if processing_tier not in PROCESSING_TIERS:
            raise ValueError(f"unknown processing_tier: {processing_tier}")
        ref = str(entity_ref or "").strip()
        if not ref:
            raise ValueError("entity_ref is required")
        by_id = None
        if ENTRY_ID.match(ref):
            by_id = self.get(ref)
            if by_id is None or by_id["blackhole_id"] != ref:
                raise LookupError(NO_SUCH_ENTRY)

        # By its own id an entry keeps its entity, its name and its aliases exactly as stored.
        entity_id, canonical_name, aliases_json = (
            (by_id["entity_id"], None, "[]") if by_id else self._resolve_entity(ref))
        names = {normalize_entity_name(str(alias)) for alias in aliases if str(alias or "").strip()} - {""}
        marked = {normalize_entity_name(str(value)) for value in identifiers if str(value or "").strip()} - {""}
        entity_names = set(_normalized_aliases(aliases_json))
        if names or marked:
            # Stored normalized, as a re-flag below stores them, so a second identical flag changes nothing.
            aliases_json = json.dumps(sorted(entity_names | names | marked))
        normalized = by_id["normalized_name"] if by_id else normalize_entity_name(canonical_name or ref)
        if not normalized:
            raise ValueError("entity_ref did not normalize to a usable name")

        existing = by_id or self.get(normalized)
        if by_id is None and starts_like_an_entry_id(ref) and (
                existing is None or existing["blackhole_id"] == normalized):
            # Nearly an entry's id (capitals, a digit too many, the bare prefix): nothing is made under it, and it
            # does not reach an entry by that entry's id in another letter case either.
            raise LookupError(NO_SUCH_ENTRY)
        if existing:
            # Idempotent: already protected. Refresh the mutable bits, do not
            # restart a rebuild that may already have completed.
            # A re-flag must not forget owner-saved aliases when the entity was
            # reaped or its current resolver inventory has become narrower.
            aliases_json = json.dumps(sorted(set(existing["aliases"]) | set(_normalized_aliases(aliases_json))))
            marked = self._marked(existing, names=entity_names | names, identifiers=marked)
            with with_db_write():
                self._conn.execute(
                    """
                    UPDATE entity_blackholes
                    SET processing_tier=?, note=COALESCE(?, note), entity_id=?,
                        canonical_name=COALESCE(?, canonical_name),
                        aliases_json=?, updated_at=datetime('now')
                    WHERE blackhole_id=?
                    """,
                    (
                        processing_tier,
                        note,
                        entity_id or existing["entity_id"],
                        canonical_name,
                        aliases_json,
                        existing["blackhole_id"],
                    ),
                )
                self._write_marked(existing["blackhole_id"], marked, was=existing["identifier_aliases"])
                notification_id = None
                if not carried and has_waiting(existing):
                    notification_id = self._make_full(existing)
                commit_connection(self._conn)
            record = self.get(existing["blackhole_id"]) or {}
            return {**record, "already_blackholed": True, "notification_id": notification_id}

        # A new entry named by a handle or an id (a contact with no usable name): its own name is an identifier.
        own_name = set() if (not entity_id and normalized in marked and normalized not in names) else {normalized}
        marked -= entity_names | names | own_name
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
            self._write_marked(blackhole_id, marked, was=[])
            if carried:
                # Carried and waiting: every name and identifier of the entry, and the entry itself. No notice of
                # its own (the step writes one for all of them), and no rebuild is owed until the owner acts.
                self._write_waiting(blackhole_id, whole=True,
                                    terms={normalized, *_normalized_aliases(aliases_json)})
                notification_id = None
            else:
                # D4: the notification is raised *before* the rebuild, so the owner
                # knows the hide is not yet complete across derived artifacts.
                notification_id = self._notify(
                    blackhole_id=blackhole_id,
                    entity_id=entity_id,
                    normalized_name=normalized,
                    kind="rebuild_needed",
                    message=notice or (
                        f"'{canonical_name or ref}' is now off-limits. A rebuild is needed before it "
                        "disappears from summaries, briefs and digests; until then those are withheld "
                        "from everyone but you."
                    ),
                )
            commit_connection(self._conn)
            _purge_message_search(self._conn)
        record = self.get(normalized) or {}
        return {**record, "already_blackholed": False, "notification_id": notification_id}

    @staticmethod
    def _marked(existing: Dict[str, Any], *, names: Set[str], identifiers: Set[str]) -> Set[str]:
        """Which aliases of an existing entry are identifiers once `names` and `identifiers` are added. A name wins:
        an alias the entry already had as a name, its own name, and every name given now are never marked, so
        nothing here can make the boundary stop reading a real name as one."""
        was = set(existing.get("identifier_aliases") or [])
        kept_names = (set(existing["aliases"]) - was) | set(names)
        if existing["normalized_name"] not in was:
            kept_names.add(existing["normalized_name"])
        return (was | set(identifiers)) - kept_names

    def _write_marked(self, blackhole_id: str, marked: Set[str], *, was: Sequence[str]) -> None:
        """Store the identifier list of one entry, inside the caller's write. Nothing is written, and the column is
        not added, for an entry that has none and had none."""
        if not marked and not was:
            return
        ensure_identifier_aliases(self._conn)
        self._conn.execute(
            f"UPDATE entity_blackholes SET {IDENTIFIERS_COLUMN}=? WHERE blackhole_id=?",
            (json.dumps(sorted(marked)), blackhole_id),
        )

    def _write_waiting(self, blackhole_id: str, *, whole: bool, terms: Set[str], entity_id: str = "") -> None:
        """Store what of one entry is carried and waiting (WAITING_COLUMN), inside the caller's write."""
        ensure_waiting_column(self._conn)
        mark: Dict[str, Any] = {"whole": bool(whole), "terms": sorted(term for term in terms if term)}
        if entity_id:
            mark["entity_id"] = str(entity_id)
        self._conn.execute(
            f"UPDATE entity_blackholes SET {WAITING_COLUMN}=? WHERE blackhole_id=?",
            (json.dumps(mark), blackhole_id),
        )

    def bind_carried_entity(self, *, entity_ref: str, entity_id: str) -> bool:
        """Link an entry that has no entity to one, for the upgrade step: every reader that serves someone else
        gains the id join at once, and the link waits for the owner like the names the step added (the OWNER view
        still reads the entry without an entity). False when the entry has an entity already."""
        record = self.get(entity_ref)
        if record is None or record["entity_id"] or not str(entity_id or "").strip():
            return False
        with with_db_write():
            self._conn.execute(
                "UPDATE entity_blackholes SET entity_id=?, updated_at=datetime('now') WHERE blackhole_id=?",
                (str(entity_id), record["blackhole_id"]),
            )
            self._write_waiting(record["blackhole_id"], whole=record["carried_waiting"],
                                terms=set(record["carried_waiting_aliases"]), entity_id=str(entity_id))
            commit_connection(self._conn)
            _purge_message_search(self._conn)
        return True

    def _make_full(self, record: Dict[str, Any]) -> str:
        """The owner's act on an entry that has anything carried and waiting, inside the caller's write (ruling
        P.3): nothing of it waits any more, so from here every reader sees an ordinary entry. Its clean-up is owed
        again, and D4 holds as for a new entry: the owner is told before it runs. Returns the notice's id."""
        self._conn.execute(
            f"UPDATE entity_blackholes SET {WAITING_COLUMN}=NULL, rebuild_state='pending', "
            "updated_at=datetime('now') WHERE blackhole_id=?",
            (record["blackhole_id"],),
        )
        self._resolve_notifications(record["blackhole_id"], kinds=("rebuild_complete", "rebuild_needed"))
        from .off_limits_list import display_label

        notification_id = self._notify(
            blackhole_id=record["blackhole_id"],
            entity_id=record["entity_id"],
            normalized_name=record["normalized_name"],
            kind="rebuild_needed",
            message=(
                f"'{display_label(self._conn, record)}' is now fully Off-limits. A rebuild is needed before it "
                "disappears from summaries, briefs and digests; until then those are withheld from everyone "
                "but you."
            ),
        )
        self._settle_carry_notice()
        return notification_id

    def make_full(self, entity_ref: str) -> Optional[str]:
        """Make an entry that is carried and waiting (or has waiting names) a full entry: the owner's act. Returns
        the id of the notice it raised, or None when there is no such entry or nothing of it waited."""
        record = self.get(entity_ref)
        if record is None or not has_waiting(record):
            return None
        with with_db_write():
            notification_id = self._make_full(record)
            commit_connection(self._conn)
            _purge_message_search(self._conn)
        return notification_id

    def waiting_count(self) -> int:
        """How many entries have anything carried and waiting: one per person the upgrade carried and the owner has
        not acted on."""
        return sum(1 for record in self.list() if has_waiting(record))

    def note_carried_over(self, message: str) -> str:
        """The upgrade step's ONE notice: written once, and its words replaced by a later run of the step while it
        is still open. Inside the caller's write."""
        row = self._conn.execute(
            "SELECT notification_id FROM blackhole_notifications WHERE blackhole_id=? AND kind=? AND state='open'",
            (CARRY_NOTICE_ID, CARRIED_OVER),
        ).fetchone()
        if row is not None:
            self._conn.execute("UPDATE blackhole_notifications SET message=? WHERE notification_id=?",
                               (message, row[0]))
            return str(row[0])
        return self._notify(blackhole_id=CARRY_NOTICE_ID, entity_id="", normalized_name="", kind=CARRIED_OVER,
                            message=message)

    def _settle_carry_notice(self) -> None:
        """Resolve the step's notice once no entry is carried and waiting any more. Inside the caller's write, after
        the change that may have ended the last one."""
        if not any(has_waiting(record) for record in self.list()):
            self._conn.execute(
                "UPDATE blackhole_notifications SET state='resolved', resolved_at=datetime('now') "
                "WHERE blackhole_id=? AND kind=? AND state='open'",
                (CARRY_NOTICE_ID, CARRIED_OVER),
            )

    def add_aliases(
        self,
        *,
        entity_ref: str,
        aliases: Sequence[str] = (),
        identifiers: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """Give an entry that exists further names and identifiers, and nothing else (review R1 node, R-L4).

        For the upgrade step that carries an older per-person exclude onto an entry the owner had already made. The
        owner's own tier, note, entity and clean-up state stay as they are. What the entry gains is CARRIED AND
        WAITING (WAITING_COLUMN): the share boundary reads the new names and identifiers at once, like any alias,
        and every reader that serves the owner himself does not see them until the owner acts on the entry. The
        entry stays a full entry for the names it had. Until the third fix round such an entry was put back to
        `pending`, which withheld every summary from the owner's own outside client and routines for as long as
        the owner did nothing: a change to an owner-serving path made by an unattended step. Returns the record
        with `grew`; an entry that gains nothing is not written at all.
        """
        record = self.get(entity_ref)
        if record is None:
            raise ValueError("no such off-limits entry")
        names = {normalize_entity_name(str(alias)) for alias in aliases if str(alias or "").strip()} - {""}
        given = {normalize_entity_name(str(value)) for value in identifiers if str(value or "").strip()} - {""}
        merged = set(record["aliases"]) | names | given
        marked = self._marked(record, names=names, identifiers=given)
        if merged == set(record["aliases"]) and marked == set(record["identifier_aliases"]):
            return {**record, "grew": False, "notification_id": None}
        gained = merged - set(record["aliases"]) - {record["normalized_name"]}
        with with_db_write():
            self._conn.execute(
                "UPDATE entity_blackholes SET aliases_json=?, updated_at=datetime('now') WHERE blackhole_id=?",
                (json.dumps(sorted(merged)), record["blackhole_id"]),
            )
            self._write_marked(record["blackhole_id"], marked, was=record["identifier_aliases"])
            self._write_waiting(record["blackhole_id"], whole=record["carried_waiting"],
                                terms=set(record["carried_waiting_aliases"]) | gained,
                                entity_id=record["carried_waiting_entity_id"])
            commit_connection(self._conn)
            _purge_message_search(self._conn)
        return {**(self.get(entity_ref) or {}), "grew": True, "notification_id": None}

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
            from .off_limits_list import display_label

            notification_id = self._notify(
                blackhole_id=record["blackhole_id"],
                entity_id=record["entity_id"],
                normalized_name=record["normalized_name"],
                kind="reinclude_needed",
                message=(
                    # An entry that only ever waited changed no summary, so none has to be rebuilt.
                    f"'{display_label(self._conn, record)}' is no longer Off-limits and can be shared again."
                    if record["carried_waiting"] else
                    f"'{display_label(self._conn, record)}' is no longer "
                    "off-limits. A rebuild is needed before it reappears in summaries and digests."
                ),
            )
            self._settle_carry_notice()
            commit_connection(self._conn)
            _purge_message_search(self._conn)
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

    def rebind_dead_entity_ids(self) -> int:
        """Re-point black holes whose stored entity_id no longer exists.

        A black hole stores the entity_id it was created against. If that entity
        is later reaped and the name is then re-extracted from a record, the
        spine mints a NEW id — and the black hole goes on excluding a row that
        is gone while the live entity carrying the withdrawn name matches
        nothing. The name terms still cover it, which is why this is a narrowing
        of protection rather than a loss of it, but the id join is the primary
        filter and it silently covers nothing.

        Found on the owner's node 2026-08-27: one of three black holes pointed
        at a deleted id while an entity with the identical canonical name sat in
        the spine under a fresh one.

        Matches on the stored normalization AND one freshly derived from
        ``canonical_name``, because ``normalize_entity_name`` has changed since
        some rows were written — see :meth:`renormalize_stored_names`. Only ever
        re-points to an entity whose name matches; it never invents a binding.
        """
        try:
            rows = self._conn.execute(
                "SELECT blackhole_id, entity_id, normalized_name, canonical_name"
                " FROM entity_blackholes WHERE entity_id != ''"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if self._legacy_table_missing(exc):
                return 0
            raise
        rebound = 0
        with with_db_write():
            for blackhole_id, entity_id, normalized_name, canonical_name in rows:
                alive = self._conn.execute(
                    "SELECT 1 FROM entities WHERE entity_id=?", (str(entity_id),)
                ).fetchone()
                if alive:
                    continue
                candidates = {
                    str(normalized_name or ""),
                    normalize_entity_name(str(canonical_name or "")),
                }
                candidates.discard("")
                if not candidates:
                    continue
                placeholders = ",".join("?" for _ in candidates)
                match = self._conn.execute(
                    f"SELECT entity_id FROM entities WHERE normalized_name IN ({placeholders})"
                    " ORDER BY mention_count DESC LIMIT 1",
                    tuple(candidates),
                ).fetchone()
                if not match:
                    continue
                self._conn.execute(
                    "UPDATE entity_blackholes SET entity_id=?, updated_at=datetime('now')"
                    " WHERE blackhole_id=?",
                    (str(match[0]), str(blackhole_id)),
                )
                rebound += 1
            commit_connection(self._conn)
        return rebound

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

    def mark_rebuild_complete(self, entity_ref: str, *, too_short: int = 0) -> bool:
        """The clean-up ran to its end. `too_short` is how many of the ways the person could be written it never
        looks for (a name or handle under three characters). With any, the notice does not say "fully hidden
        everywhere": the person is hidden from everyone else by the read-time checks, and text that names them
        only that way was not cleaned out of the owner's own summaries (review R2-L4: a contact saved as "J" was
        reported complete by a clean-up that had looked for nothing that names them)."""
        record = self.get(entity_ref)
        if record is None:
            return False
        self._set_rebuild_state(entity_ref, "complete")
        from .off_limits_list import display_label

        label = display_label(self._conn, record)
        with with_db_write():
            self._resolve_notifications(record["blackhole_id"], kinds=("rebuild_needed",))
            self._notify(
                blackhole_id=record["blackhole_id"],
                entity_id=record["entity_id"],
                normalized_name=record["normalized_name"],
                kind="rebuild_complete",
                message=(
                    f"'{label}' is Off-limits and hidden from everyone else. {too_short} of its names "
                    f"{'is' if too_short == 1 else 'are'} too short to look for in your summaries, so text that "
                    "names them only that way was not cleaned out."
                    if too_short else
                    f"'{label}' is now fully hidden "
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
            if self._legacy_table_missing(exc):
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
            if self._legacy_table_missing(exc):
                return ("", ref, "[]")
            raise
        if row is None:
            return ("", ref, "[]")
        return (str(row[0]), row[1], row[2] or "[]")


# ------------------------------------------------------- module-level reads
# Mirrors `exclusions.excluded_record_ids`: cheap helpers the read paths call
# without constructing a store.


def blackholed_entity_ids(conn: sqlite3.Connection, *, view: str = EVERYONE) -> Set[str]:
    return BlackholeStore(conn).blackholed_entity_ids(view=view)


def blackholed_name_terms(conn: sqlite3.Connection, *, view: str = EVERYONE) -> Set[str]:
    return BlackholeStore(conn).blackholed_name_terms(view=view)


def off_limits_terms(conn: sqlite3.Connection, *, view: str = EVERYONE) -> OffLimitsTerms:
    return BlackholeStore(conn).terms(view=view)


def pending_rebuild_names(conn: sqlite3.Connection, *, view: str = EVERYONE) -> Set[str]:
    return BlackholeStore(conn).pending_rebuild_names(view=view)


def secure_providers_for(conn: sqlite3.Connection, entity_ref: str) -> frozenset:
    """Providers allowed to process content mentioning this entity (D1).

    An entity that is not black-holed places no constraint, signalled by the
    empty frozenset — callers read that as "no restriction", not "nothing allowed".
    """
    tier = BlackholeStore(conn).processing_tier(entity_ref)
    if tier is None:
        return frozenset()
    return TIER_PROVIDERS[tier]
