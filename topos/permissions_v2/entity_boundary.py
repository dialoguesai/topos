"""Snapshot-local Off-limits veto for the registered fact/message family.

This checks observed identities, complete stored text surfaces and contact-linked
conversation context. It is NOT semantic entity coverage or an NER absence proof.
Indirect references with no recorded/name/contact association remain a residual
of this contract. Missing schemas, malformed JSON and unbounded context withhold.
No recipient or model can supply this object, its terms or its context.
"""
from __future__ import annotations

import html
import re
import sqlite3
import unicodedata
from collections import defaultdict, deque

from .canonical import PolicyError, Rows, digest, digest_stream

VERSION = "node-observed-entity-boundary/v2"
UNAVAILABLE = "entity_protection_lineage_unavailable"
MAX_ROWS = 100_000
MAX_CONTEXT_ROWS = 10_000
MAX_SURFACE_BYTES = 2_097_152
MAX_DEPTH = 32
# A deliberately pinned small set, not a claim of Unicode confusable coverage.
CONFUSABLES = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "м": "m",
    "у": "y", "і": "i", "ј": "j", "Α": "a", "Β": "b", "Ε": "e", "Η": "h", "Ι": "i",
    "Κ": "k", "Μ": "m", "Ν": "n", "Ο": "o", "Ρ": "p", "Τ": "t", "Χ": "x",
    "α": "a", "β": "b", "ε": "e", "η": "h", "ι": "i", "κ": "k", "μ": "m", "ν": "n",
    "ο": "o", "ρ": "p", "τ": "t", "χ": "x"})


def normalized(value: str) -> str:
    value = html.unescape(value).translate(CONFUSABLES)
    value = unicodedata.normalize("NFKD", value).casefold().translate(CONFUSABLES)
    return "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() else ch
                   for ch in value if unicodedata.category(ch) not in {"Mn", "Mc", "Me", "Cf"})


def skeleton(value: str) -> str:
    return "".join(ch for ch in normalized(value) if ch.isalnum())


def _strings(value, depth=0):
    if depth > MAX_DEPTH:
        raise PolicyError(UNAVAILABLE)
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, child in value.items() for text in [str(key), *_strings(child, depth + 1)]]
    if isinstance(value, list):
        return [text for child in value for text in _strings(child, depth + 1)]
    if value is None or type(value) in (int, float, bool):
        return []
    raise PolicyError(UNAVAILABLE)


def _decode(value):
    from .evidence import _json
    try:
        # JSON scalar cells are unsupported: registered metadata and inventories
        # are objects/arrays. Duplicate keys and nonfinite values also refuse.
        return _json(value, (dict, list))
    except PolicyError:
        raise PolicyError(UNAVAILABLE) from None


def surfaces(row: dict) -> list[str]:
    result, size = [], 0
    for key, value in row.items():
        if key.startswith("_p2b_"):
            continue
        if value is None:
            continue
        if isinstance(value, bytes):
            # Unsupported valued binary content cannot be silently skipped.
            raise PolicyError(UNAVAILABLE)
        if isinstance(value, str):
            size += len(value.encode("utf-8"))
            if size > MAX_SURFACE_BYTES:
                raise PolicyError(UNAVAILABLE)
            result.extend(_strings(_decode(value)) if key.endswith("_json") and value else [value])
        elif type(value) not in (int, float, bool):
            raise PolicyError(UNAVAILABLE)
    return result


def rows_revision(groups):
    # Bounded native groups, streamed so a populated entity spine is not
    # accidentally capped by the signed-request grammar's 1 MiB limit.
    return digest_stream({"version": VERSION, "groups": Rows(
        [index, len(group), *sorted(digest({key: repr(value) for key, value in row.items()}) for row in group)]
        for index, group in enumerate(groups))})


class EntityBoundary:
    """One canonical SQLite read transaction; never retained between reads."""

    def __init__(self, conn):
        self.conn = conn
        self.ids, self.contacts, self.terms, self.handles = set(), set(), set(), set()
        self._context_cache = {}
        try:
            flags = self._table("entity_blackholes", {"entity_id", "normalized_name", "canonical_name", "aliases_json"})
            self.active = bool(flags)
            if not self.active:
                self.revision = digest({"version": VERSION, "active": False})
                return
            entities = self._table("entities", {"entity_id", "canonical_name", "normalized_name", "aliases_json", "identifiers_json", "contact_id"}, projected=True)
            merges = self._table("entity_merge_tombstones", {"absorbed_entity_id", "merged_into", "canonical_name", "aliases_json", "identifiers_json"})
            contacts = self._table("contacts", {"contact_id", "display_name"})
            identifiers = self._table("contact_identifiers", {"contact_id", "identifier", "identifier_type"})
            for flag in flags:
                if not skeleton(flag["normalized_name"]) or not isinstance(flag["entity_id"], str):
                    raise PolicyError(UNAVAILABLE)
                if flag["entity_id"]:
                    self.ids.add(flag["entity_id"])
                self._names(flag)
            self._close_identities(entities, merges, contacts, identifiers)
            self._mentions_by_record = {}
            for mention in self.mentions:
                self._mentions_by_record.setdefault(mention["record_id"], []).append(mention)
            self.terms.discard("")
            # Recompute the closure against the full current universe, but bind
            # only the protection decisions it produces. Unrelated enrichment
            # must not invalidate every grant. New protected aliases, reminted
            # IDs, merges, contact links and mentions still change this digest.
            self.revision = digest({"version": VERSION, "revision_contract": "protected-closure/v2",
                "ids": sorted(self.ids), "contacts": sorted(self.contacts),
                "terms": sorted(self.terms), "handles": sorted(self.handles),
                "mentions": rows_revision([self.mentions])})
        except (sqlite3.Error, TypeError, ValueError, RecursionError):
            raise PolicyError(UNAVAILABLE) from None

    def _close_identities(self, entities, merges, contacts, identifiers):
        """Visit each recorded association once, including learned mention aliases.

        Repeated whole-universe scans made a reverse-ordered merge chain take
        quadratic work. Queued ids/names retain the same conservative closure;
        newly learned mention spellings also close reminted entities/contacts.
        """
        by_id, by_name, by_contact, handles = (defaultdict(list) for _ in range(4))
        entity_rows = [*entities, *merges]
        entity_names = []
        for index, row in enumerate(entity_rows):
            names = set(filter(None, map(skeleton, self._name_values(row))))
            entity_names.append(names)
            for key in ("entity_id", "absorbed_entity_id", "merged_into"):
                if row.get(key):
                    by_id[row[key]].append(index)
            for name in names:
                by_name[name].append(index)
        contact_names = defaultdict(list)
        for index, row in enumerate(contacts):
            by_contact[row["contact_id"]].append(index)
            contact_names[skeleton(row["display_name"] or "")].append(index)
        for row in identifiers:
            handles[row["contact_id"]].append(row["identifier"])
        sets = {"id": self.ids, "term": self.terms, "contact": self.contacts}
        queue = deque((kind, value) for kind, values in sets.items() for value in values)

        def add(kind, values):
            for value in values:
                if value and value not in sets[kind]:
                    sets[kind].add(value)
                    queue.append((kind, value))

        seen_entities, seen_contacts, queried_ids = set(), set(), set()
        self.mentions = []
        mention_columns = {"entity_id", "record_id", "source_id", "canonical_table", "surface_text"}
        self._table("entity_mentions", mention_columns, where="WHERE 0")
        while True:
            while queue:
                kind, value = queue.popleft()
                linked_entities = by_id[value] if kind == "id" else by_name[value] if kind == "term" else ()
                for index in linked_entities:
                    if index in seen_entities:
                        continue
                    seen_entities.add(index)
                    row = entity_rows[index]
                    add("id", (row.get(key) for key in ("entity_id", "absorbed_entity_id", "merged_into")))
                    add("term", entity_names[index])
                    add("contact", [row.get("contact_id")])
                    if row.get("identifiers_json"):
                        values = _decode(row["identifiers_json"])
                        if not isinstance(values, list) or any(not isinstance(handle, str) for handle in values):
                            raise PolicyError(UNAVAILABLE)
                        for handle in values:
                            add("term", self._handle(handle))
                linked_contacts = by_contact[value] if kind == "contact" else contact_names[value] if kind == "term" else ()
                for index in linked_contacts:
                    if index in seen_contacts:
                        continue
                    seen_contacts.add(index)
                    row = contacts[index]
                    add("contact", [row["contact_id"]])
                    add("term", [skeleton(row["display_name"] or "")])
                    if row.get("known_usernames_json"):
                        names = _decode(row["known_usernames_json"])
                        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                            raise PolicyError(UNAVAILABLE)
                        add("term", map(skeleton, names))
                if kind == "contact":
                    for handle in handles[value]:
                        add("term", self._handle(handle))
            pending = sorted(self.ids - queried_ids)
            if not pending:
                break
            # Bounded batches work on SQLite builds with a 999-variable ceiling.
            for start in range(0, len(pending), 400):
                batch = pending[start:start + 400]
                marks = ",".join("?" for _ in batch)
                found = self._table("entity_mentions", mention_columns,
                    where=f"WHERE entity_id IN ({marks})", args=tuple(batch), limit=MAX_ROWS - len(self.mentions))
                self.mentions.extend(found)
                for mention in found:
                    if mention["surface_text"]:
                        add("term", [skeleton(mention["surface_text"])])
            queried_ids.update(pending)

    def _table(self, table, required, *, where="", args=(), limit=MAX_ROWS, projected=False):
        schema = self.conn.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchmany(2)
        columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if len(schema) != 1 or schema[0][0] != "table" or not required <= columns:
            raise PolicyError(UNAVAILABLE)
        fields = ",".join(sorted(required)) if projected else "*"
        cursor = self.conn.execute(f"SELECT {fields} FROM {table} {where}", args)
        names = [column[0] for column in cursor.description]
        rows = cursor.fetchmany(limit + 1)
        if len(rows) > limit:
            raise PolicyError(UNAVAILABLE)
        return [dict(zip(names, tuple(row))) for row in rows]

    @staticmethod
    def _name_values(row):
        names = [row[key] for key in ("normalized_name", "canonical_name") if row.get(key)]
        if row.get("aliases_json") is not None:
            aliases = _decode(row["aliases_json"])
            if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
                raise PolicyError(UNAVAILABLE)
            names.extend(aliases)
        if any(not isinstance(name, str) for name in names):
            raise PolicyError(UNAVAILABLE)
        return names

    def _names(self, row):
        self.terms.update(filter(None, map(skeleton, self._name_values(row))))

    def _handle(self, value):
        if not isinstance(value, str) or not value.strip():
            raise PolicyError(UNAVAILABLE)
        key = skeleton(value)
        if not key:
            raise PolicyError(UNAVAILABLE)
        keys = {key}
        plain = normalized(value)
        digits = "".join(ch for ch in plain if ch.isdecimal())
        if len(digits) >= 10 and all(ch.isdecimal() or ch in "+-(). " for ch in plain):
            keys.add(digits[-10:])
        self.handles.update(keys)
        return keys

    def _hits(self, row):
        texts = surfaces(row)
        # Initials and short names must not match every occurrence inside a
        # larger word ("M.E." in "message"). Full names/handles also get the
        # separator-free scan, which catches URLs and invisible punctuation.
        long_terms = [term for term in self.terms if len(term) >= 4]
        short_terms = self.terms.difference(long_terms)
        for text in texts:
            plain = normalized(text)
            compact = "".join(ch for ch in plain if ch.isalnum())
            tokens = {skeleton(token) for token in re.split(r"[\s@:/<>]+", plain)}
            tokens.update(skeleton(token) for token in re.findall(r"[^\W_]+", plain))
            if short_terms.intersection(tokens) or any(term in compact for term in long_terms):
                return True
        return False

    def _linked(self, record_id, table, source_id, *, any_source=False):
        # Unknown legacy table labels are veto signals, not evidence that the
        # match belongs to a different supported family. Dataset collisions
        # similarly withhold because the mention schema has no dataset key.
        known = {"signal_objects", "conversation_messages", "ai_chat_messages", "conversations", "ai_chat_conversations"}
        return any(mention["record_id"] == record_id and mention["entity_id"] in self.ids
            and (mention["canonical_table"] not in known or mention["canonical_table"] == table)
            and (any_source or mention["source_id"] in (None, "", source_id)) for mention in self._mentions_by_record.get(record_id, ()))

    def _context(self, table, row, source_id, dataset_id):
        conversation = row.get("conversation_id")
        if not isinstance(conversation, str) or not conversation:
            raise PolicyError(UNAVAILABLE)
        key = (table, source_id, dataset_id, conversation)
        if key in self._context_cache:
            return self._context_cache[key]
        if table == "conversation_messages":
            args = (conversation, source_id, dataset_id)
            where = "WHERE conversation_id=? AND source_id=? AND dataset_id=?"
            parent = self._table("conversations", {"conversation_id", "source_id", "dataset_id"}, where=where, args=args, limit=1)
            roster = self._table("conversation_participants", {"conversation_id", "source_id", "dataset_id", "contact_id"}, where=where, args=args, limit=MAX_CONTEXT_ROWS)
            self._table(table, {"conversation_id", "source_id", "dataset_id", "sender_id"}, where="WHERE 0")
            siblings = [{"sender_id": item[0]} for item in self.conn.execute(
                f"SELECT DISTINCT sender_id FROM {table} {where} AND sender_id IS NOT NULL", args).fetchmany(MAX_CONTEXT_ROWS + 1)]
        elif table == "ai_chat_messages":
            args = (conversation, source_id)
            where = "WHERE conversation_id=? AND source_id=?"
            parent = self._table("ai_chat_conversations", {"conversation_id", "source_id", "owner_user_id"}, where=where, args=args, limit=1)
            roster = []
            siblings = []
        else:
            raise PolicyError(UNAVAILABLE)
        if len(parent) != 1 or len(siblings) > MAX_CONTEXT_ROWS:
            raise PolicyError(UNAVAILABLE)
        rows = [*parent, *roster, *siblings]
        matched = any(item.get("contact_id") in self.contacts or item.get("sender_id") in self.contacts
                      or item.get("sender_id") in self.ids or self._hits(item) for item in rows)
        # Parent/ancestor record protection is a direct veto even without names.
        parent_table = "conversations" if table == "conversation_messages" else "ai_chat_conversations"
        matched |= self._linked(conversation, parent_table, source_id)
        matched |= self.conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=?",
                                     (parent_table, conversation)).fetchone() is not None
        revision = rows_revision([rows])
        self._context_cache[key] = (matched, revision)
        return matched, revision

    def _reply_context(self, table, row, source_id, dataset_id):
        """Only declared reply ancestors, never every nearby message's content."""
        rows, seen = [], set()
        reference = row.get("reply_to_message_id")
        while reference:
            if not isinstance(reference, str) or reference in seen or len(seen) >= MAX_DEPTH:
                raise PolicyError(UNAVAILABLE)
            seen.add(reference)
            where, args = "WHERE message_id=? AND source_id=? AND conversation_id=?", [reference, source_id, row.get("conversation_id")]
            if table == "conversation_messages":
                where += " AND dataset_id=?"
                args.append(dataset_id)
            found = self._table(table, {"message_id", "source_id", "conversation_id", "content"}, where=where, args=args, limit=1)
            if len(found) != 1:
                raise PolicyError(UNAVAILABLE)
            parent = found[0]
            if self.conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=?",
                                 (table, reference)).fetchone() is not None:
                raise PolicyError("entity_protected")
            if self._linked(reference, table, source_id):
                raise PolicyError("entity_protected")
            rows.append(parent)
            reference = parent.get("reply_to_message_id")
        return rows

    def observe(self, *, table, record_id, source_id, dataset_id, row):
        """Private owner preview may observe a veto; it cannot authorize release."""
        if not self.active:
            return False, self.revision
        try:
            matched = self._hits(row)
            # Legacy identities that omit table/source can veto by record id;
            # they never prove a negative association.
            matched |= self._linked(record_id, table, source_id)
            if table == "signal_objects":
                payload = _decode(row.get("payload_json"))
                if not isinstance(payload, dict):
                    raise PolicyError(UNAVAILABLE)
                matched |= any(payload.get(key) in self.ids for key in ("subject_entity_id", "object_entity_id"))
                context_revision = self.revision
            else:
                context_matched, context_revision = self._context(table, row, source_id, dataset_id)
                matched |= context_matched
                replies = self._reply_context(table, row, source_id, dataset_id)
                matched |= any(self._hits(parent) for parent in replies)
                context_revision = digest({"context": context_revision, "replies": rows_revision([replies])})
            return matched, digest({"boundary": self.revision, "context": context_revision})
        except (sqlite3.Error, TypeError, ValueError, RecursionError):
            raise PolicyError(UNAVAILABLE) from None

    def check(self, **kwargs):
        """Veto the entire evidence item; return a private context revision."""
        matched, revision = self.observe(**kwargs)
        if matched:
            raise PolicyError("entity_protected")
        return revision

    def mentions_protected(self, *texts) -> bool:
        """Whether any of these texts carries an Off-limits term: the same match ``legacy_veto`` applies
        to a row's surfaces. For derived text (a claim a model is asked about) that has no row of its own."""
        if not self.active:
            return False
        return self._hits({f"text_{i}": text for i, text in enumerate(texts) if isinstance(text, str)})

    def legacy_veto(self, table, row):
        """Observed native rows, before legacy projection/redaction.

        This is a veto only: it grants no permission and does not qualify a v2
        fact. Message context is recovered from its exact canonical identity,
        since legacy public rows can omit parent and sender fields.
        """
        if not self.active:
            return False
        texts = surfaces(row)
        if self._hits(row) or any(text in self.ids or text in self.contacts for text in texts):
            return True
        record_id = next((row.get(key) for key in ("record_id", "message_id", "id", "event_id", "entry_id", "contact_id", "entity_id") if row.get(key)), None)
        if record_id and self._linked(record_id, table, row.get("source_id"), any_source=row.get("source_id") is None):
            return True
        if table not in {"conversation_messages", "ai_chat_messages", "message_stream"}:
            return False
        if not isinstance(record_id, str) or not record_id:
            raise PolicyError(UNAVAILABLE)
        found = []
        for native_table in (["conversation_messages", "ai_chat_messages"] if table == "message_stream" else [table]):
            where, args = "WHERE message_id=?", [record_id]
            for key in ("source_id", "dataset_id"):
                if row.get(key) is not None and (key != "dataset_id" or native_table == "conversation_messages"):
                    where += f" AND {key}=?"
                    args.append(row[key])
            found.extend((native_table, native) for native in self._table(native_table,
                {"message_id", "source_id", "conversation_id", "content"}, where=where, args=args, limit=1))
        if len(found) != 1:
            raise PolicyError(UNAVAILABLE)
        native_table, native = found[0]
        matched, _revision = self.observe(table=native_table, record_id=record_id,
            source_id=native.get("source_id"), dataset_id=native.get("dataset_id"), row=native)
        return matched
