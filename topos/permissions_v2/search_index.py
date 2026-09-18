"""The per-grant permitted-set index behind p2c-v1 search. Built owner-side only.

P(g) = the terminal messages of reviewed facts whose p2a decision under grant g
is `permit`, computed with the same qualification (`_qualified_bundle`, floors
first, then the review, then `_eligible`) and the same decision function
(`release.source_message_decision`) the locator door uses. A recipient request
never builds or widens this set; it only reads it to rank candidates, and every
candidate is decided again at release (search_release.py). A defect here can cost
availability, never access.

What an index file holds, per member: its opaque record id, its event time, a
term bag (term -> count) and its chunk vectors. That is everything ranking needs
and nothing else. The member's canonical identity and witness fact ids, needed
only by the release re-check and the sweep, are sealed with AES-GCM under a key
derived from the grant's record-id key, which lives in a different file. No raw
content, sender field or row id is readable from an index file in its own words; the
term bags and vectors are content derivatives (a bag of words is most of a message),
so the file is treated as content at rest: 0600
in a 0700 directory, never WAL, and are zero-overwritten before unlink.

The index is a scrub surface. It is deleted, not merely marked stale, when the
protection clock moves, when a member's canonical row is deleted or scrubbed,
and when the grant is revoked, expires or changes policy. `purge_for_database`
is called from the black-hole and source-scrub lifecycles; `sweep` compares
every file against the ledger and the canonical database; the request path
refuses whatever is missing.

MERGE GATE (design review, 18 Sep): the build holds the node write gate for
O(reviewed facts). Before any run on a copy of the owner's database it must build
on a read snapshot outside the gate and take the gate only to publish.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import struct
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from topos.disclosure.content_policy import is_record_nsfw
from topos.principal import OWNER_APP, current_principal
from topos.storage.db.write_gate import with_db_write

from .canonical import PolicyError, canonical_bytes, parse_json
from .evidence import _key
from .fact_eligibility import canonical_utc_microseconds
from .identity import ATTESTED_CONTRACT
from .opaque_ids import RecordKeys, opaque_record_id, private_directory, private_file, seal_key
from .protection_clock import clock_state
from .search_contract import CAPABILITY_SEARCH

FORMAT = "topos-p2c-index/v1"
ROOT_NAME = "message-search"
_TOKEN = re.compile(r"\w{2,}")
STOPWORDS = frozenset("""
a an and are as at be but by for from has have i if in into is it its me my of on or our so that the their them
then there these they this to was we were what when where which who why will with you your do did does not no
""".split())
_DDL = (
    "CREATE TABLE meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), format TEXT NOT NULL, basis_json TEXT NOT NULL,"
    " state TEXT NOT NULL, model TEXT, dims INTEGER, member_count INTEGER NOT NULL)",
    "CREATE TABLE members (opaque_id TEXT PRIMARY KEY, event_at_us INTEGER, doc_len INTEGER NOT NULL,"
    " terms_json TEXT NOT NULL, sealed BLOB NOT NULL)",
    "CREATE TABLE vectors (opaque_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, vector BLOB NOT NULL,"
    " PRIMARY KEY (opaque_id, chunk_index))",
)


def tokenize(text: str) -> list[str]:
    """NFKC, casefold, Unicode word runs of two or more characters, minus a small stop list."""
    if not isinstance(text, str):
        return []
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return [token for token in _TOKEN.findall(normalized) if token not in STOPWORDS]


def root_for(canonical_database: Path) -> Path:
    return Path(canonical_database).parent / "permissions-v2" / ROOT_NAME


def index_path(root: Path, grant_id: str) -> Path:
    return Path(root) / ("grant-" + hashlib.sha256(grant_id.encode("utf-8")).hexdigest()[:32] + ".db")


def _shred(path: Path) -> None:
    """Zero-overwrite then unlink. Never raises for a missing file."""
    for candidate in (path, path.with_name(path.name + "-journal")):
        try:
            size = candidate.stat().st_size
        except FileNotFoundError:
            continue
        try:
            with open(candidate, "r+b", buffering=0) as handle:
                handle.write(b"\0" * size)
                os.fsync(handle.fileno())
        finally:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def purge(root: Path, grant_id: str) -> None:
    _shred(index_path(root, grant_id))


def purge_all(root: Path) -> int:
    """Delete every index file under `root`; keys stay (they hold no content)."""
    root = Path(root)
    if not root.is_dir():
        return 0
    removed = 0
    for path in sorted(root.glob("grant-*.db")) + sorted(root.glob(".build-*.db")):
        _shred(path)
        removed += 1
    return removed


def purge_for_database(canonical_database) -> int:
    """Lifecycle hook: a protection change or scrub on this database drops every search index.

    Best effort and silent: the black-hole and scrub lifecycles must never fail
    because search is not configured. The request path refuses a missing index.
    """
    try:
        if isinstance(canonical_database, sqlite3.Connection):
            rows = canonical_database.execute("PRAGMA database_list").fetchall()
            canonical_database = next((row[2] for row in rows if row[1] == "main"), "")
        if not canonical_database:
            return 0
        return purge_all(root_for(Path(canonical_database)))
    except Exception:  # noqa: BLE001 -- a hook must not break the owner's lifecycle
        return 0


def _f32(vector) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unf32(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


@dataclass(frozen=True)
class Member:
    opaque_id: str
    event_at_us: int | None
    doc_len: int
    terms: dict
    sealed: bytes


@dataclass(frozen=True)
class LoadedIndex:
    basis: dict
    model: str | None
    dims: int | None
    members: tuple[Member, ...]
    vectors: dict  # opaque_id -> list of chunk vectors


def basis_of(authority, *, clock) -> dict:
    return {"format": FORMAT, "grant_id": authority.grant_id, "assignment_id": authority.assignment_id,
            "grant_generation": authority.grant_generation, "assignment_generation": authority.assignment_generation,
            "policy_hash": authority.policy_hash, "protection_revision": authority.protection_revision,
            "clock_id": clock[0], "clock_generation": clock[1]}


def seal(key: bytes, opaque_id: str, value: dict) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(seal_key(key)).encrypt(nonce, canonical_bytes(value), opaque_id.encode("ascii"))


def unseal(key: bytes, opaque_id: str, blob: bytes) -> dict:
    try:
        return parse_json(AESGCM(seal_key(key)).decrypt(blob[:12], blob[12:], opaque_id.encode("ascii")))
    except Exception:  # noqa: BLE001 -- tampered, foreign or rotated-key member
        raise PolicyError("search_index_integrity") from None


def row_digest(row) -> str:
    """Every column of one canonical row, as stored. Any change after a build shows up here."""
    if row is None:
        return ""
    items = sorted((str(key), repr(value)) for key, value in dict(row).items())
    return hashlib.sha256(json.dumps(items, ensure_ascii=True).encode("ascii")).hexdigest()


def _live_rows(conn, member: dict):
    """The member's row and its witness facts' rows, read the way evidence reads them (SELECT *)."""
    conn.row_factory = sqlite3.Row
    table = member["table"]
    if table not in ("conversation_messages", "ai_chat_messages"):
        raise PolicyError("search_index_integrity")
    rows = conn.execute(f"SELECT * FROM {table} WHERE message_id=? AND source_id=?",
                        (member["record_id"], member["source_id"])).fetchmany(2)
    facts = [conn.execute("SELECT * FROM signal_objects WHERE object_id=?", (fact_id,)).fetchmany(2)
             for fact_id in member["facts"]]
    return rows, facts


def _member_fingerprint(rows, facts) -> str | None:
    """None when the record or a witness fact is gone, duplicated or marked deleted (evidence._deleted)."""
    from .evidence import _deleted
    if len(rows) != 1 or _deleted(dict(rows[0])) or any(len(found) != 1 or _deleted(dict(found[0])) for found in facts):
        return None
    return hashlib.sha256("|".join([row_digest(rows[0])] + [row_digest(found[0]) for found in facts]).encode()).hexdigest()


def _default_model() -> str | None:
    try:
        from topos.engine.backends.huggingface import active_embedding_model
        return active_embedding_model()
    except Exception:  # noqa: BLE001
        return None


class SearchIndexService:
    """Owner-side builder, sweeper and read-only loader of per-grant indexes."""

    def __init__(self, *, ledger, resolver, reviews, root: Path, embedding_model=_default_model):
        if reviews.binding != resolver.binding or ledger.identity.model_dump() != resolver.binding.model_dump():
            raise PolicyError("search_index_binding")
        self.ledger, self.resolver, self.reviews = ledger, resolver, reviews
        self.root = private_directory(Path(root))
        self.keys = RecordKeys(self.root)
        self.embedding_model = embedding_model

    # -- owner side ---------------------------------------------------------

    @staticmethod
    def _require_owner(binding) -> None:
        principal = current_principal()
        if (principal is None or principal.cls != OWNER_APP or principal.channel not in {"uds", "cp_relay"}
                or principal.acting_user != binding.owner_id):
            raise PolicyError("owner_authority_required")

    def _search_grants(self, now: int) -> list[str]:
        with self.ledger._transaction() as db:
            rows = db.execute("SELECT grant_id FROM p2a_grants").fetchall()
            found = []
            for row in rows:
                try:
                    _, policy = self.ledger._authority(db, row["grant_id"], now)
                except PolicyError:
                    found.append(row["grant_id"])  # inactive/expired: listed so it is forgotten
                    continue
                if policy.versions.capability == CAPABILITY_SEARCH:
                    found.append(row["grant_id"])
            return found

    def forget(self, grant_id: str) -> None:
        """Revoke/expiry: delete the index and rotate the id key."""
        purge(self.root, grant_id)
        self.keys.delete(grant_id)

    def rebuild_all(self, *, now: int | None = None) -> dict:
        self._require_owner(self.resolver.binding)
        now = int(time.time()) if now is None else now
        states = {}
        for grant_id in self._search_grants(now):
            try:
                states[grant_id] = self.rebuild(grant_id, now=now)["state"]
            except Exception:  # noqa: BLE001 -- a failed rebuild leaves no index for that grant
                purge(self.root, grant_id)
                states[grant_id] = "failed"
        return states

    def rebuild(self, grant_id: str, *, now: int | None = None) -> dict:
        """Owner-only. Returns only a state and a count; never a reason or an id.

        The whole build, publish included, holds the (re-entrant) node write gate, so
        two rebuilds cannot publish out of order and a sweep never races a publish.
        """
        self._require_owner(self.resolver.binding)
        with with_db_write():
            return self._rebuild(grant_id, now=now)

    def _rebuild(self, grant_id: str, *, now: int | None = None) -> dict:
        from .release import source_message_decision

        now = int(time.time()) if now is None else now
        with self.ledger._transaction() as db:
            try:
                authority, policy = self.ledger._authority(db, grant_id, now)
            except PolicyError:
                authority = policy = None
        if policy is None:
            self.forget(grant_id)            # revoked or expired: index gone, id key rotated
            return {"state": "removed", "member_count": 0}
        if policy.versions.capability != CAPABILITY_SEARCH:
            purge(self.root, grant_id)       # never rotate another capability's record-id key
            return {"state": "removed", "member_count": 0}
        key = self.keys.get(grant_id, create=True)
        tables = set(policy.search.tables)
        model = self.embedding_model() if self.embedding_model else None
        members: dict[str, dict] = {}
        with with_db_write():
            if (self.reviews.binding != self.resolver.binding
                    or self.reviews.canonical_file_revision != self.resolver._file_revision()):
                raise PolicyError("review_database_binding")
            with self.resolver._read() as (conn, floor):
                self.reviews._observe_clock(conn)
                clock = clock_state(conn)
                with self.reviews._db() as review_db:
                    fact_ids = sorted({row[0] for row in review_db.execute(
                        "SELECT fact_id FROM fact_reviews NOT INDEXED WHERE active=1")})
                    for fact_id in fact_ids:
                        try:
                            qualified, rows = self.resolver._qualified_bundle(conn, floor, fact_id, self.reviews,
                                review_db, contract=ATTESTED_CONTRACT, discloses_sources=True)
                            decision = source_message_decision(policy, qualified)
                        except PolicyError:
                            continue
                        if decision.verdict != "permit":
                            continue
                        for leaf in qualified.snapshot.leaves:
                            identity = leaf.identity
                            row = rows[_key(identity)]
                            # Never releasable by search, so never in its statistics: an
                            # NSFW-flagged or undated record is left out of R(g) entirely.
                            # A rolling window only moves forward: a record already older than
                            # it can never be released again, so its term bag is not kept either.
                            event_us = canonical_utc_microseconds(row.get("event_at"))
                            if (identity.table not in tables or is_record_nsfw(row) or event_us is None
                                    or event_us < (now - policy.search.window.max_age_seconds) * 1_000_000):
                                continue
                            entry = members.setdefault(_key(identity), {"identity": identity, "facts": set(),
                                                                        "row": row})
                            entry["facts"].add(fact_id)
                    over_cap = len(members) > policy.search.max_permitted_records
                    built = [] if over_cap else self._members(conn, key, grant_id, members, model)
        if floor != authority.protection_revision:
            # The owner changed protection state and has not re-synced this grant; its
            # requests refuse until then, and no index is kept for a stale authority.
            purge(self.root, grant_id)
            return {"state": "stale", "member_count": 0}
        basis = basis_of(authority, clock=clock)
        dims = next((len(vector) for _, _, _, vectors in built for vector in vectors), None)
        self._publish(grant_id, basis, "over_cap" if over_cap else "ready", model if dims else None, dims, built)
        return {"state": "over_cap" if over_cap else "ready", "member_count": 0 if over_cap else len(built)}

    def _members(self, conn, key, grant_id, members, model):
        built = []
        has_embeddings = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_embeddings'").fetchone() is not None
        for entry in members.values():
            identity, row = entry["identity"], entry["row"]
            opaque = opaque_record_id(key, grant_id=grant_id, table=identity.table, source_id=identity.source_id,
                                      dataset_id=identity.dataset_id, record_id=identity.record_id)
            tokens = tokenize(row.get("content") or "")
            terms: dict[str, int] = {}
            for token in tokens:
                terms[token] = terms.get(token, 0) + 1
            vectors = []
            if has_embeddings and model:
                from topos.features.signal.vector_codec import decode_vector
                for blob, fmt in conn.execute(
                        "SELECT vector_blob, vector_format FROM signal_embeddings WHERE source_id=? AND record_id=? "
                        "AND model=? AND vector_blob IS NOT NULL ORDER BY chunk_index", (identity.source_id,
                        identity.record_id, model)):
                    try:
                        vectors.append([float(value) for value in decode_vector(blob, fmt or "json")])
                    except Exception:  # noqa: BLE001 -- an undecodable vector is simply absent
                        continue
                if len({len(vector) for vector in vectors}) > 1:
                    vectors = []
            event_us = canonical_utc_microseconds(row.get("event_at"))
            member_fields = {"table": identity.table, "source_id": identity.source_id,
                             "dataset_id": identity.dataset_id, "record_id": identity.record_id,
                             "facts": sorted(entry["facts"])}
            fingerprint = _member_fingerprint(*_live_rows(conn, member_fields))
            if fingerprint is None:
                continue
            sealed = seal(key, opaque, {**member_fields, "fingerprint": fingerprint})
            built.append((Member(opaque, event_us, len(tokens), terms, sealed), opaque, identity, vectors))
        built.sort(key=lambda item: item[1])
        return built

    def _publish(self, grant_id, basis, state, model, dims, built) -> None:
        final = index_path(self.root, grant_id)
        temporary = self.root / (".build-" + os.urandom(8).hex() + ".db")
        private_file(temporary)
        try:
            conn = sqlite3.connect(str(temporary), isolation_level=None)
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("BEGIN")
                for sql in _DDL:
                    conn.execute(sql)
                conn.execute("INSERT INTO meta VALUES (1, ?, ?, ?, ?, ?, ?)", (FORMAT, canonical_bytes(basis).decode("ascii"),
                             state, model, dims, len(built)))
                for member, opaque, _identity, vectors in built:
                    conn.execute("INSERT INTO members VALUES (?, ?, ?, ?, ?)", (opaque, member.event_at_us, member.doc_len,
                                 json.dumps(member.terms, sort_keys=True, ensure_ascii=False), member.sealed))
                    for index, vector in enumerate(vectors):
                        conn.execute("INSERT INTO vectors VALUES (?, ?, ?)", (opaque, index, _f32(vector)))
                conn.execute("COMMIT")
            finally:
                conn.close()
            with open(temporary, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, final)
            os.chmod(final, 0o600)
        except BaseException:
            _shred(temporary)
            raise

    # -- sweep: the index is a scrub surface --------------------------------

    def sweep(self, *, now: int | None = None, on_error: str = "purge") -> int:
        """Delete every index that no longer matches the ledger, the clock or its rows. Never raises.

        Under the write gate, so a sweep never deletes a file a concurrent rebuild just published.
        """
        with with_db_write():
            return self._sweep(now=now, on_error=on_error)

    def _sweep(self, *, now: int | None = None, on_error: str = "purge") -> int:
        now = int(time.time()) if now is None else now
        removed = 0
        try:
            files = sorted(self.root.glob("grant-*.db"))
        except OSError:
            return 0
        if not files:
            return 0
        try:
            with self.ledger._transaction() as db:
                authorities = {}
                for row in db.execute("SELECT grant_id FROM p2a_grants").fetchall():
                    try:
                        authorities[index_path(self.root, row["grant_id"]).name] = (
                            row["grant_id"], self.ledger._authority(db, row["grant_id"], now)[0])
                    except PolicyError:
                        authorities[index_path(self.root, row["grant_id"]).name] = (row["grant_id"], None)
            conn = sqlite3.connect(self.resolver.path.as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("BEGIN")
                clock = clock_state(conn)
                for path in files:
                    grant_id, authority = authorities.get(path.name, (None, None))
                    if not self._current(path, grant_id, authority, clock, conn):
                        if grant_id is not None and authority is None:
                            self.forget(grant_id)
                        else:
                            _shred(path)
                        removed += 1
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            # Owner hooks and the daemon: if the check cannot run, no index survives it. A recipient
            # request instead refuses, so one caller's transient error never empties other grants.
            if on_error == "raise":
                raise PolicyError("search_index_sweep_unavailable") from None
            removed += purge_all(self.root)
        return removed

    def _current(self, path, grant_id, authority, clock, conn) -> bool:
        if grant_id is None or authority is None or authority.capability_version != CAPABILITY_SEARCH:
            return False
        try:
            index = self._open(path)
        except PolicyError:
            return False
        expected = basis_of(authority, clock=clock)
        basis = dict(index["basis"])
        if {k: v for k, v in basis.items() if k != "protection_revision"} != \
                {k: v for k, v in expected.items() if k != "protection_revision"}:
            return False
        key = self.keys.get(grant_id, create=False)
        if key is None:
            return False
        for opaque, sealed in index["sealed"]:
            try:
                member = unseal(key, opaque, sealed)
                # Deleted, scrubbed, edited, re-flagged or superseded since the build: the index no
                # longer describes R(g), so it goes (the owner's next rebuild restores search).
                if _member_fingerprint(*_live_rows(conn, member)) != member["fingerprint"]:
                    return False
            except (PolicyError, sqlite3.Error, KeyError):
                return False
        return True

    def check_own(self, grant_id: str, authority, *, now: int) -> None:
        """The request path's check: this grant's file only, O(|R(g)|). Refuses; never purges others."""
        path = index_path(self.root, grant_id)
        try:
            conn = sqlite3.connect(self.resolver.path.as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("BEGIN")
                current = path.exists() and self._current(path, grant_id, authority, clock_state(conn), conn)
            finally:
                conn.close()
        except (sqlite3.Error, PolicyError):
            raise PolicyError("search_index_unavailable") from None
        if not current:
            if path.exists():
                with with_db_write():
                    _shred(path)
            raise PolicyError("search_index_stale")

    # -- request side: read only --------------------------------------------

    @staticmethod
    def _open(path: Path) -> dict:
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error:
            raise PolicyError("search_index_missing") from None
        try:
            meta = conn.execute("SELECT format, basis_json, state, model, dims, member_count FROM meta WHERE singleton=1").fetchone()
            if meta is None or meta[0] != FORMAT:
                raise PolicyError("search_index_integrity")
            sealed = conn.execute("SELECT opaque_id, sealed FROM members ORDER BY opaque_id").fetchall()
            return {"basis": parse_json(meta[1]), "state": meta[2], "model": meta[3], "dims": meta[4],
                    "count": meta[5], "sealed": sealed, "conn_path": path}
        except sqlite3.Error:
            raise PolicyError("search_index_integrity") from None
        finally:
            conn.close()

    def load(self, grant_id: str, authority) -> LoadedIndex:
        """The grant's index, or a refusal. Reads only the index file."""
        path = index_path(self.root, grant_id)
        if not path.exists():
            raise PolicyError("search_index_missing")
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error:
            raise PolicyError("search_index_missing") from None
        try:
            meta = conn.execute("SELECT format, basis_json, state, model, dims, member_count FROM meta WHERE singleton=1").fetchone()
            if meta is None or meta[0] != FORMAT:
                raise PolicyError("search_index_integrity")
            basis = parse_json(meta[1])
            if meta[2] != "ready":
                raise PolicyError("search_index_over_cap")
            for field in ("grant_id", "assignment_id", "grant_generation", "assignment_generation", "policy_hash",
                          "protection_revision"):
                if basis.get(field) != getattr(authority, field):
                    raise PolicyError("search_index_stale")
            members = tuple(Member(row[0], row[1], row[2], json.loads(row[3]), row[4]) for row in conn.execute(
                "SELECT opaque_id, event_at_us, doc_len, terms_json, sealed FROM members ORDER BY opaque_id"))
            if len(members) != meta[5]:
                raise PolicyError("search_index_integrity")
            vectors: dict[str, list] = {}
            for opaque, _chunk, blob in conn.execute("SELECT opaque_id, chunk_index, vector FROM vectors ORDER BY opaque_id, chunk_index"):
                vectors.setdefault(opaque, []).append(_unf32(blob))
            return LoadedIndex(basis, meta[3], meta[4], members, vectors)
        except (sqlite3.Error, ValueError, TypeError, struct.error):
            raise PolicyError("search_index_integrity") from None
        finally:
            conn.close()
