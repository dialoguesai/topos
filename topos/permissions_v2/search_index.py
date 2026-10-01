"""The per-grant permitted-set index behind p2c-v1 search. Built owner-side only.

P(g) = the terminal messages of qualifying facts whose p2a decision under grant g
is `permit`, computed with the same qualification (`_qualified_bundle`, floors
first, then the review -- explicit, or implicit unless the owner deselected the
fact -- then `_eligible`) and the same decision function
(`release.source_message_decision`) the locator door uses. Every current fact is
a candidate; the owner's opt-outs are the only rows the review store contributes. A recipient request
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

MERGE GATE (design review, 18 Sep; closed 26 Sep): the build no longer holds the
node write gate for O(facts). `_rebuild` takes the gate twice, briefly: once to
freeze the owner's decisions (explicit reviews, opt-outs, their authority digest,
the clock), then builds on an ungated read snapshot of the canonical database, and
once more to publish, after re-checking that floor, clock and the review digest are
what it froze; a change in between is retried, bounded. Measured on a synthetic
production-schema copy (300 qualifying facts, 27,000 other signal objects): see
the numbers in CHANGELOG 1.4.2 and `tests/permissions_v2/test_implicit_review.py`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import struct
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from topos.disclosure.content_policy import is_record_nsfw
from topos.principal import OWNER_APP, current_principal
from topos.storage.db.write_gate import with_db_write

from .canonical import PolicyError, canonical_bytes, parse_json
from .evidence import _COPY_COUNT, _key, _row_revision
from .fact_eligibility import canonical_utc_microseconds
from .identity import ATTESTED_CONTRACT
from .opaque_ids import RecordKeys, opaque_record_id, private_directory, private_file, seal_key
from .protection_clock import clock_state
from .search_contract import (CAPABILITY_MESSAGE_SEARCH, SEARCH_CAPABILITIES, CAPABILITY_SEARCH,
    CAPABILITY_KNOWLEDGE_SEARCH, DIRECT_SEARCH_CAPABILITIES)

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


def forget_inactive_record_keys(ledger, root, *, now: int) -> int:
    """Revoked or expired grants lose their record-id key, whatever their capability.

    The locator view's opaque ids (p2a-v3) share this one store, so a revoke must rotate
    them too, with or without message search enabled. Also shreds any index such a grant
    still has. Returns how many keys went; a node with no store does nothing.
    """
    root = Path(root)
    if ledger is None or not (root / "keys.db").exists():
        return 0
    keys = RecordKeys(root)
    removed = 0
    for grant_id in keys.grant_ids():
        with ledger._transaction() as db:
            try:
                ledger._authority(db, grant_id, now)
                continue
            except PolicyError:
                pass
        purge(root, grant_id)
        keys.delete(grant_id)
        removed += 1
    return removed


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
class DerivedIdentity:
    """The identity an index seals for a member derived from many rows (an IF-5 interest), which has no row of
    its own: the fields `_members` mints its opaque id from, nothing else."""
    table: str
    source_id: str
    dataset_id: None
    record_id: str


@dataclass(frozen=True)
class LoadedIndex:
    basis: dict
    model: str | None
    dims: int | None
    members: tuple[Member, ...]
    vectors: dict  # opaque_id -> list of chunk vectors


def basis_of(authority, *, clock, boundary_revision=None) -> dict:
    return {"format": FORMAT, "grant_id": authority.grant_id, "assignment_id": authority.assignment_id,
            "grant_generation": authority.grant_generation, "assignment_generation": authority.assignment_generation,
            "policy_hash": authority.policy_hash, "protection_revision": authority.protection_revision,
            "clock_id": clock[0], "clock_generation": clock[1], "entity_boundary_revision": boundary_revision}


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
    if member["table"] == "journal_entries":
        return _live_journal_rows(conn, member)
    table = member["table"]
    if table not in ("conversation_messages", "ai_chat_messages"):
        raise PolicyError("search_index_integrity")
    rows = conn.execute(f"SELECT * FROM {table} WHERE message_id=? AND source_id=?",
                        (member["record_id"], member["source_id"])).fetchmany(2)
    facts = [conn.execute("SELECT * FROM signal_objects WHERE object_id=?", (fact_id,)).fetchmany(2)
             for fact_id in member["facts"]]
    return rows, facts


def _family_rubric_basis() -> dict:
    """The assessment revisions of the families beyond messages, when they exist (IF-5 §5).

    Empty with every such family off, so a messages-only index's basis is byte for byte what it was,
    and a journal-only one keeps the bytes it had before interests. A journal rubric or floor change,
    or an interest label rubric change, moves this, and the index is rebuilt; so does turning a
    family's flag on or off.
    """
    from . import interest_index, interest_review
    from .automatic_message_review import rubric_revision_for
    from .evidence_families import family
    revisions = {}
    if family("journal_entries").enabled():
        revisions["journal_entry"] = rubric_revision_for("journal_entries")
    if interest_index.enabled():
        revisions["interest"] = interest_review.rubric_revision()
    return {"automatic_rubric_revisions": revisions} if revisions else {}


def _live_journal_rows(conn, member: dict):
    """A journal member's row and the facts naming it, as `_live_rows` returns a message's (IF-5)."""
    from .evidence_families import enabled_family
    try:
        enabled_family("journal_entries")
    except PolicyError:
        return [], []   # the family is off: the member no longer exists
    rows = conn.execute("SELECT * FROM journal_entries WHERE entry_id=? AND source_id=?",
                        (member["record_id"], member["source_id"])).fetchmany(2)
    facts = [conn.execute("SELECT * FROM signal_objects WHERE object_id=?", (fact_id,)).fetchmany(2)
             for fact_id in member.get("facts", ())]
    return rows, facts


def _member_fingerprint(rows, facts, table: str = "conversation_messages") -> str | None:
    """The reviewed surface (evidence._row_revision) of the record and of each witness fact.

    Exactly what the access decision reads, NSFW flag included; operational columns
    a sync rewrites are not, so they never refuse search. None when the record or a
    witness fact is gone, duplicated or marked deleted (evidence._deleted).
    """
    from .evidence import _deleted, _row_revision
    if len(rows) != 1 or _deleted(dict(rows[0])) or any(len(found) != 1 or _deleted(dict(found[0])) for found in facts):
        return None
    parts = [_row_revision(dict(rows[0]), table=table)] + [_row_revision(dict(found[0]), table="signal_objects")
                                                           for found in facts]
    return hashlib.sha256("|".join(parts).encode("ascii")).hexdigest()


def _lineage_net(conn, member: dict) -> list:
    """(object_id, payload) of every fact naming the member's own record, as the floors decide it.

    Only the payload is kept, as before: a naming fact's other columns (read by `_floors`'
    boundary check, or a closed `valid_to`) were never in the hash; release re-checks them.
    A member whose witness fact or projection cites several messages is watched through its
    own record only. A fact naming one of the others leaves the index in place until the next
    rebuild, and the sibling floor at release, which reads every leaf, still refuses it.
    """
    from .message_evidence import facts_naming
    return sorted((object_id, payload or "") for object_id, payload, _refs in
                  facts_naming(conn, {member["record_id"]: {member["table"]}}))


def _lineage_fingerprint(conn, member: dict, content) -> str:
    """The rows the sibling-fact and independent-copy floors read: every fact naming the record,
    with its disclosure, and the number of identical copies across both message tables.

    They run only in the owner-side and daemon sweeps, never on a recipient's request path
    (design §7 R1, R2); drift is dropped within one sweep. Both reads are the floors' own and
    both are keyed. The facts are exactly the ones `_names_a_leaf` says name the record
    (`message_evidence.facts_naming`, which `_floors` and the sibling floor read), asked of the
    migration-78 keys. The net before this read every fact on the node per member per sweep,
    and held every fact whose references carry a JSON escape, so one such write anywhere
    changed every member's hash and dropped the whole index. The copy count is
    `evidence._COPY_COUNT`, answered from the migration-76 content key: the integer the bare
    `content=?` count gave, since a row equal to the text has its length and first 64
    characters too.
    """
    conn.row_factory = sqlite3.Row
    citing = _lineage_net(conn, member)
    copies = sum(conn.execute(_COPY_COUNT.format(table=table), (content,)).fetchone()[0]
                 for table in ("conversation_messages", "ai_chat_messages")) if isinstance(content, str) else -1
    from .evidence_families import family
    if isinstance(content, str) and family("journal_entries").enabled():
        # A journal twin appearing later withholds a member as a message twin does (IF-5 §1.2).
        copies += conn.execute("SELECT count(*) FROM journal_entries WHERE content=?", (content,)).fetchone()[0]
    return hashlib.sha256(json.dumps([citing, copies], ensure_ascii=True).encode("ascii")).hexdigest()


def _default_model() -> str | None:
    try:
        from topos.engine.backends.huggingface import active_embedding_model
        return active_embedding_model()
    except Exception:  # noqa: BLE001
        return None


def local_passage_embedder(text: str, model: str):
    """Owner-side local inference; never send evidence to a hosted provider.

    Resolve only an already downloaded model. The ordinary query adapter then
    uses the same model identifier and its query prefix at read time.
    """
    from huggingface_hub import snapshot_download
    from sentence_transformers import SentenceTransformer
    from topos.engine.backends.huggingface import apply_embedding_prefix
    from topos.engine.model_cache import ModelSlot, get_model_cache
    from topos.engine.torch_runtime import device_for

    path = snapshot_download(repo_id=model, local_files_only=True)
    device = device_for("embeddings")
    handle, _ = get_model_cache().acquire(ModelSlot.EMBEDDING, f"{model}@{device}",
        lambda: SentenceTransformer(path, device=device, local_files_only=True, trust_remote_code=False))
    passages = apply_embedding_prefix([text], model_name=model, input_role="passage")
    return handle.encode(passages, convert_to_numpy=True, normalize_embeddings=True,
                         show_progress_bar=False)[0].tolist()


def _file_state(path) -> tuple | None:
    """Every stat field a write by any means moves, and the file's identity; None when absent."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_uid)


def _lstat_state(path: Path) -> tuple | None:
    """A path's own identity and mode, not following a link, as `_snapshot`'s lstat checks (N5 review, R1).

    Not its mtime or ctime: those move whenever any grant's file or row is written, which would make the send token
    move on another grant's activity. A swap for a link, a replacement or a chmod still moves it."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid)


def _provenance_gate_wait(point: str | None):
    """A provenance pass's two gate entries as IF-3 v1.4 names them: `<point>_setup` (its one service) and `<point>` (its one `_check`)."""
    from . import search_timing
    return lambda site: search_timing.gate_wait(None if point is None else point + "_setup" if site == "setup" else point)


class SearchVerification:
    """One search's Off-limits closure and verified review digest, reused across its own stages only.

    WS4 N3a. A search validates its grant's index three times: at index load (`check_own`), in the
    gated recheck (`_current`, then every candidate's re-decision), and at send (`check_own` again).
    Each pass built its own EntityBoundary, a read of the whole entity spine; the gated pass built
    two (one in `_current`, one through the resolver for the re-decision). Each pass also read the
    review digest, and the two `check_own` reads entered the write gate to do it. Here the closure
    and the digest are computed once, and a later stage gets them only when a token proves nothing
    they were computed from has changed since.

    - Canonical token: `PRAGMA data_version` on a dedicated read-only probe connection, which
      SQLite changes whenever any other connection commits, plus the stat state of the database
      file and of its WAL and journal sidecars. A write by any means, or a file replaced
      underneath, moves the stat state.
    - Review token: the same over the review store, plus the in-memory state `_db` compares the
      store with: the rollback floor's expected digest and the store's clock high-water.

    A value is kept only when the token read BEFORE its snapshot was established equals the one
    read after. It is reused only when the token read after the next stage's own snapshot was
    established equals the kept one. So reuse implies no commit between the two snapshots: they
    hold the same rows. A token that cannot be read is None, and None never matches, so the
    stage recomputes in full, exactly as before.

    What is NOT reused: every per-member check (dependencies and their provenance, live rows,
    classification contexts, the boundary's per-record context, fingerprints) and every candidate
    re-decision run on each stage's own snapshot. A reused closure is re-bound to that stage's
    connection with an empty context cache. A reused digest still passes the store's own file
    checks (`_check_file`) in that stage.
    """

    def __init__(self, resolver, reviews):
        self._resolver, self._reviews = resolver, reviews
        # The stages run on different threads, one after another; the lock only orders a late
        # close (a cancelled search) against a probe still in use. A closed state never reopens.
        self._lock = threading.Lock()
        self._closed = False
        self._probes: dict[str, sqlite3.Connection] = {}
        self._boundary = None  # (canonical token, EntityBoundary)
        self._digest = None    # (review token, digest)
        self._send = None      # N5: the gated recheck's state, for the send check (SearchIndexService.send_token)
        self.reused = {"boundary": 0, "digest": 0, "send": 0}
        self.computed = {"boundary": 0, "digest": 0, "send": 0}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            probes, self._probes = self._probes, {}
            self._boundary = self._digest = self._send = None
            for probe in probes.values():
                try:
                    probe.close()
                except sqlite3.Error:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _data_version(self, path: Path) -> int:
        with self._lock:
            if self._closed:
                raise PolicyError("search_verification_closed")
            key = str(path)
            probe = self._probes.get(key)
            if probe is None:
                # Never holds a transaction: each PRAGMA is its own brief read, so no writer waits on it.
                probe = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, isolation_level=None,
                                        check_same_thread=False)
                self._probes[key] = probe
            return probe.execute("PRAGMA data_version").fetchone()[0]

    def _rows(self, path: Path, statements) -> tuple:
        """N5: the rows `statements` select, read in one brief read transaction on the probe connection of `path`."""
        with self._lock:
            if self._closed:
                raise PolicyError("search_verification_closed")
            key = str(path)
            probe = self._probes.get(key)
            if probe is None:
                probe = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, isolation_level=None,
                                        check_same_thread=False)
                self._probes[key] = probe
            probe.execute("BEGIN")
            try:
                return tuple(tuple(tuple(row) for row in probe.execute(sql, args).fetchall()) for sql, args in statements)
            finally:
                probe.execute("COMMIT")

    @staticmethod
    def _files(path: Path) -> tuple:
        main = _file_state(path)
        if main is None:
            raise FileNotFoundError(path)
        return (main, _file_state(Path(str(path) + "-wal")), _file_state(Path(str(path) + "-journal")))

    def canonical_token(self) -> tuple | None:
        try:
            path = self._resolver.path
            self._resolver._incarnation()
            return ("canonical", self._data_version(path), self._files(path))
        except Exception:  # noqa: BLE001 -- unreadable: never matches, the stage recomputes
            return None

    def review_token(self) -> tuple | None:
        try:
            reviews = self._reviews
            floor = reviews._floor
            expected = floor.expected_authority_digest() if floor is not None else None
            return ("reviews", self._data_version(reviews.path), self._files(reviews.path), expected,
                    reviews._highest_generation, reviews.canonical_file_revision, reviews._file_identity)
        except Exception:  # noqa: BLE001 -- unreadable: never matches, the stage recomputes
            return None

    def boundary(self, conn, *, before):
        """The Off-limits boundary for `conn`'s snapshot. `before`: canonical_token() read before it was established."""
        after = self.canonical_token()
        kept = self._boundary
        if kept is not None and after is not None and kept[0] == after:
            self.reused["boundary"] += 1
            boundary = kept[1].rebind(conn)
        else:
            from .entity_boundary import EntityBoundary
            self.computed["boundary"] += 1
            boundary = EntityBoundary(conn)
            self._boundary = (after, boundary) if after is not None and before == after else None
        # Inside a resolver read, the re-decision asks the resolver for this snapshot's boundary:
        # hand it this one, so the gated pass reads the closure once, not twice.
        cache = getattr(self._resolver, "_entity_boundaries", None)
        if isinstance(cache, dict) and conn in cache and cache[conn] is None:
            cache[conn] = boundary
        return boundary

    def digest(self, *, point: str | None = None) -> str:
        """The review store's authority digest, verified against its rollback floor by `_db`, or the one it verified."""
        token = self.review_token()
        kept = self._digest
        if kept is not None and token is not None and kept[0] == token:
            self._reviews._check_file()
            self.reused["digest"] += 1
            return kept[1]
        from . import search_timing
        self.computed["digest"] += 1
        with search_timing.gate_wait(point):  # the digest enters the gate (evidence.py `_db`)
            value = self._reviews.current_authority_digest()
        after = self.review_token()
        self._digest = (after, value) if after is not None and token == after else None
        return value

    def keep_send_token(self, before: dict | None, after: dict | None) -> None:
        """N5: keep the gated recheck's state for the send check, only when nothing but its own checkpoint moved it.

        `before` is read (under the gate) before the recheck's snapshot was established, `after` after its
        checkpoint, still under the gate. Every part but the ledger, which the checkpoint itself writes, must be
        equal: then the rows and files the recheck proved are the ones `after` describes. Otherwise nothing is kept,
        and the send check runs its member loop in full.
        """
        same = (before is not None and after is not None
                and {k: v for k, v in before.items() if k != "ledger"} == {k: v for k, v in after.items() if k != "ledger"})
        self._send = after if same else None

    def send_unchanged(self, token: dict | None) -> bool:
        """N5: whether the send check's own read of the state (under the gate) is the one the recheck kept."""
        kept = self._send
        unchanged = kept is not None and token is not None and token == kept
        (self.reused if unchanged else self.computed)["send"] += 1
        return unchanged


class SearchIndexService:
    """Owner-side builder, sweeper and read-only loader of per-grant indexes."""

    def __init__(self, *, ledger, resolver, reviews, root: Path, embedding_model=_default_model, passage_embedder=None):
        if reviews.binding != resolver.binding or ledger.identity.model_dump() != resolver.binding.model_dump():
            raise PolicyError("search_index_binding")
        self.ledger, self.resolver, self.reviews = ledger, resolver, reviews
        self.root = private_directory(Path(root))
        self.keys = RecordKeys(self.root)
        self.embedding_model = embedding_model
        self.passage_embedder = passage_embedder

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
                if policy.versions.capability in SEARCH_CAPABILITIES:
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

        The gate is taken to freeze the owner's decisions and again to publish; the build
        between them runs on an ungated read snapshot (MERGE GATE, module docstring). Two
        rebuilds cannot publish out of order and a sweep never races a publish, because
        the publish step still runs under the (re-entrant) node write gate.
        """
        self._require_owner(self.resolver.binding)
        return self._rebuild(grant_id, now=now)

    REBUILD_ATTEMPTS = 3

    def _rebuild(self, grant_id: str, *, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else now
        with self.ledger._transaction() as db:
            try:
                authority, policy = self.ledger._authority(db, grant_id, now)
            except PolicyError:
                authority = policy = None
        if policy is None:
            self.forget(grant_id)            # revoked or expired: index gone, id key rotated
            return {"state": "removed", "member_count": 0}
        if policy.versions.capability not in SEARCH_CAPABILITIES:
            purge(self.root, grant_id)       # never rotate another capability's record-id key
            return {"state": "removed", "member_count": 0}
        key = self.keys.get(grant_id, create=True)
        for _attempt in range(self.REBUILD_ATTEMPTS):
            result = self._rebuild_once(grant_id, authority, policy, key, now=now)
            if result is not None:
                return result
        # The owner kept changing reviews or protection while the index was being built.
        purge(self.root, grant_id)
        return {"state": "stale", "member_count": 0}

    def _freeze(self):
        """Under the gate: the owner's decisions and the clock, as one consistent reading."""
        with with_db_write():
            if (self.reviews.binding != self.resolver.binding
                    or self.reviews.canonical_file_revision != self.resolver._file_revision()):
                raise PolicyError("review_database_binding")
            with self.resolver._read() as (conn, floor):
                self.reviews._observe_clock(conn)
                clock = clock_state(conn)
                with self.reviews._db() as review_db:
                    return self.reviews.freeze(review_db), floor, clock

    def _unchanged(self, frozen, floor, clock) -> bool:
        """Under the gate: nothing the build depended on moved while it ran."""
        with with_db_write():
            with self.resolver._read() as (conn, current_floor):
                if current_floor != floor or clock_state(conn) != clock:
                    return False
            return self.reviews.current_authority_digest() == frozen.authority_digest

    def _rebuild_once(self, grant_id, authority, policy, key, *, now):
        from .release import source_message_decision

        tables = set(policy.search.tables)
        model = self.embedding_model() if self.embedding_model else None
        members: dict[str, dict] = {}
        frozen, floor, clock = self._freeze()
        if floor != authority.protection_revision:
            # The owner changed protection state and has not re-synced this grant; its
            # requests refuse until then, and no index is kept for a stale authority.
            purge(self.root, grant_id)
            return {"state": "stale", "member_count": 0}
        # The build: every current fact the owner has not deselected, qualified on a read
        # snapshot the gate does not hold. Its cost is O(facts), never O(signal objects).
        with self.resolver._read(gated=False) as (conn, snapshot_floor):
            if snapshot_floor != floor:
                return None
            boundary_revision = self.resolver.entity_boundary(conn).revision
            boundary = self.resolver.entity_boundary(conn)
            dependencies = {}
            automatic = policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH
            direct = policy.versions.capability in DIRECT_SEARCH_CAPABILITIES
            from .message_evidence import OwnerMessageReview, qualify_message, qualify_automatic_message
            from .automatic_message_review import MachineMessageReview, context_for
            review_types = (OwnerMessageReview, MachineMessageReview) if automatic else (OwnerMessageReview,)
            candidates = (sorted({ _key(review.snapshot.message.identity): key for key, review in frozen.reviews.items()
                                   if isinstance(review, review_types)}.values())
                          if direct else [row[0] for row in conn.execute(
                "SELECT object_id FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL ORDER BY object_id")])
            for fact_id in candidates:
                if fact_id in frozen.opt_outs:
                    continue
                try:
                    if direct:
                        qualify = qualify_automatic_message if automatic else qualify_message
                        qualified, rows = qualify(self.resolver, conn, floor,
                            frozen.reviews[fact_id].snapshot.message.identity, frozen, None)
                    else:
                        qualified, rows = self.resolver._qualified_bundle(conn, floor, fact_id, frozen, None,
                            contract=ATTESTED_CONTRACT, discloses_sources=True)
                    decision = source_message_decision(policy, qualified)
                except PolicyError:
                    continue
                if decision.verdict != "permit":
                    continue
                closure_dependencies = {}
                if boundary.active:
                    for version in qualified.snapshot.artifacts + qualified.snapshot.leaves:
                        identity = version.identity
                        dependency = dependencies.setdefault(_key(identity), {
                            "table": identity.table, "record_id": identity.record_id,
                            "source_id": identity.source_id, "dataset_id": identity.dataset_id,
                            "revision": version.revision,
                            "context": boundary.check(table=identity.table, record_id=identity.record_id,
                                source_id=identity.source_id, dataset_id=identity.dataset_id, row=rows[_key(identity)])})
                        closure_dependencies[_key(identity)] = dependency
                for leaf in qualified.snapshot.leaves:
                    identity = leaf.identity
                    row = rows[_key(identity)]
                    # Never releasable by search, so never in its statistics: an
                    # NSFW-flagged or undated record is left out of R(g) entirely.
                    # A rolling window only moves forward: a record already older than
                    # it can never be released again, so its term bag is not kept either.
                    from .reconciliation_provenance import native_time_within
                    lower_us = (now - policy.search.window.max_age_seconds) * 1_000_000
                    if identity.table == "journal_entries":
                        # A journal row's time is its family's rule (IF-5 §1): inside only when every instant it
                        # can denote is; ranked by its stated day, never finer than it states.
                        from .evidence_families import rank_time_us, within
                        if (identity.table not in tables or is_record_nsfw(row)
                                or not within(identity.table, row, lower_us, now * 1_000_000)):
                            continue
                        rank_event = {"rank_event_us": rank_time_us(identity.table, row)}
                    else:
                        event_us = canonical_utc_microseconds(row.get("event_at"))
                        if (identity.table not in tables or is_record_nsfw(row) or event_us is None
                                or event_us < lower_us
                                or not native_time_within(row, lower_us, now * 1_000_000)):
                            continue
                        rank_event = {}
                    entry = members.setdefault(_key(identity), {"identity": identity, "facts": set(), "row": row,
                                                               "entity_dependencies": {}, **rank_event})
                    if direct:
                        entry["message"] = identity.model_dump()
                        if automatic:
                            entry["review_context_revision"] = context_for(conn, identity, row, boundary=boundary)[0]
                    else:
                        entry["facts"].add(fact_id)
                    entry["entity_dependencies"].update(closure_dependencies)
            if automatic:
                from .knowledge_projections import candidates, qualify_projection
                from .canonical import digest
                originals=list(members.values())
                for table,record_id in candidates(conn,[e['identity'] for e in originals],policy.search.result_types):
                    try:
                        projected=qualify_projection(self.resolver,conn,snapshot_floor,frozen,None,table,record_id,policy,
                            (now-policy.search.window.max_age_seconds)*1000000,now*1000000)
                        q,source_rows=projected.sources[0]
                        identity=q.snapshot.message.identity
                        dependencies={}
                        contexts=[]
                        for evidence,evidence_rows in projected.sources:
                            ref=evidence.snapshot.message
                            native=evidence_rows[_key(ref.identity)]
                            dependencies[_key(ref.identity)]={"table":ref.identity.table,"record_id":ref.identity.record_id,
                                "source_id":ref.identity.source_id,"dataset_id":ref.identity.dataset_id,
                                "revision":ref.revision,"context":boundary.check(table=ref.identity.table,
                                    record_id=ref.identity.record_id,source_id=ref.identity.source_id,
                                    dataset_id=ref.identity.dataset_id,row=native)}
                            contexts.append({'identity':ref.identity.model_dump(),'revision':context_for(conn,ref.identity,native,boundary=boundary)[0]})
                        members['projection:'+table+':'+record_id]={"identity":identity,"row":source_rows[_key(identity)],
                            "facts":set(),"message":identity.model_dump(),"entity_dependencies":dependencies,
                            "review_context_revision":context_for(conn,identity,source_rows[_key(identity)],boundary=boundary)[0],
                            "projection":{"table":table,"record_id":record_id,"revision":projected.revision},
                            "classification_contexts":contexts,"rank_text":projected.content,
                            # Each source by its family's rule (a journal entry's stated day, IF-5 §1).
                            "rank_event_us":projected.rank_time_us()}
                    except PolicyError:
                        continue
                # A raw member releases only as its own family's kind (IF-5): a message needs `message`,
                # a journal entry needs `journal_entry`. Projections are filtered at release by their kind.
                kinds = set(policy.search.result_types)
                members={k:v for k,v in members.items() if 'projection' in v
                         or ('journal_entry' if v['identity'].table == 'journal_entries' else 'message') in kinds}
            # IF-5 Q&A I7: interest records sit beside them, built on this same snapshot; the cap counts every family.
            interests = self._interest_entries(conn, policy, now, boundary, frozen.opt_outs) if automatic else {}
            over_cap = len(members) + len(interests) > policy.search.max_permitted_records
            built = [] if over_cap else self._members(conn, key, grant_id, {**members, **interests}, model)
        basis = basis_of(authority, clock=clock, boundary_revision=boundary_revision)
        if policy.versions.capability in DIRECT_SEARCH_CAPABILITIES:
            basis["message_review_revision"] = frozen.authority_digest
        if automatic:
            from .automatic_message_review import rubric_revision, MODEL_REVISION
            basis['automatic_rubric_revision']=rubric_revision()
            basis.update(_family_rubric_basis())
            basis['automatic_model_revision']=MODEL_REVISION
        dims = next((len(vector) for _, _, _, vectors in built for vector in vectors), None)
        with with_db_write():
            if not self._unchanged(frozen, floor, clock):
                return None
            with self.resolver._read() as (conn, _floor):
                boundary = self.resolver.entity_boundary(conn)
                if boundary.revision != boundary_revision:
                    return None
                checked = {}
                try:
                    changed = any(not self._entity_dependencies_current(conn, boundary, list(entry["entity_dependencies"].values()), checked)
                                  for entry in members.values())
                except PolicyError:
                    return None
                if changed:
                    return None
                if automatic:
                    from .knowledge_projections import current_revision
                    from .automatic_message_review import context_for
                    for entry in members.values():
                        projection = entry.get('projection')
                        if projection and current_revision(conn, projection['table'], projection['record_id']) != projection['revision']:
                            return None
                        contexts = entry.get('classification_contexts', []) or [
                            {'identity':entry['identity'].model_dump(), 'revision':entry['review_context_revision']}]
                        for context in contexts:
                            from .evidence import EvidenceIdentity
                            identity = EvidenceIdentity.parse(context['identity'])
                            row = self.resolver._load(conn, identity)
                            if context_for(conn, identity, row, boundary=boundary)[0] != context['revision']:
                                return None
                if interests and not self._interests_current(conn, interests.values(), policy, boundary, frozen.opt_outs):
                    return None
            self._publish(grant_id, basis, "over_cap" if over_cap else "ready", model if dims else None, dims, built)
        return {"state": "over_cap" if over_cap else "ready", "member_count": 0 if over_cap else len(built)}

    # Passage vectors a build may compute for members with no stored vector. Bounded owner
    # maintenance work: members past it stay searchable lexically, and only permitted members
    # ever reach the embedder. Measured (RD3, scripts/permissions_v2/p2c_vector_cost.py, CPU,
    # two threads, a loaded host): 8-18 ms per member, so a full bound is about 8.6 s of build
    # wall time on the ungated read snapshot, and its vectors add about 14 ms to the gated
    # publish. It was 32, which left members of an ordinary grant without vectors on every build.
    EMBEDDINGS_PER_BUILD = 1024

    def _members(self, conn, key, grant_id, members, model):
        built = []
        remaining_embeddings = self.EMBEDDINGS_PER_BUILD
        has_embeddings = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_embeddings'").fetchone() is not None
        for entry in members.values():
            identity, row = entry["identity"], entry["row"]
            opaque = opaque_record_id(key, grant_id=grant_id, table=identity.table, source_id=identity.source_id,
                                      dataset_id=identity.dataset_id, record_id=identity.record_id)
            projection=entry.get('projection')
            if projection:
                opaque=opaque_record_id(key,grant_id=grant_id,table=projection['table'],source_id=None,
                                        dataset_id=None,record_id=projection['record_id'])
            interest = entry.get("interest")   # IF-5 Q&A I7: a derived record, no stored vector
            rank_text=entry.get('rank_text',row.get('content') or '')
            tokens = tokenize(rank_text)
            terms: dict[str, int] = {}
            for token in tokens:
                terms[token] = terms.get(token, 0) + 1
            vectors = []
            if has_embeddings and model and not projection and interest is None:
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
            if not vectors and model and self.passage_embedder is not None and remaining_embeddings:
                content = rank_text
                if isinstance(content, str) and len(content.encode("utf-8")) <= 65536:
                    remaining_embeddings -= 1
                    try:
                        candidate = self.passage_embedder(content, model)
                        if (isinstance(candidate, (list, tuple)) and 0 < len(candidate) <= 4096
                                and all(type(value) in (float, int) and math.isfinite(value) for value in candidate)
                                and any(value != 0 for value in candidate)):
                            vectors = [[float(value) for value in candidate]]
                    except Exception:  # unavailable local model preserves lexical search
                        pass
            event_us = entry.get('rank_event_us',canonical_utc_microseconds(row.get("event_at")))
            member_fields = {"table": identity.table, "source_id": identity.source_id,
                             "dataset_id": identity.dataset_id, "record_id": identity.record_id,
                             "facts": sorted(entry["facts"]), **({"message": entry["message"]} if "message" in entry else {})}
            if "review_context_revision" in entry:
                member_fields["review_context_revision"] = entry["review_context_revision"]
            if projection:
                member_fields['projection']=projection
                member_fields['classification_contexts']=entry['classification_contexts']
            if interest is not None:
                # An interest has no row of its own: its fingerprint is the object's content revision, and its
                # binding is what the currency check and the release decide again (interest_index), Off-limits over
                # the label and the month's visits included. No row, boundary context or lineage to seal.
                sealed = seal(key, opaque, {**member_fields, "interest": interest,
                                            "fingerprint": interest["content_revision"]})
                built.append((Member(opaque, event_us, len(tokens), terms, sealed), opaque, identity, vectors))
                continue
            fingerprint = _member_fingerprint(*_live_rows(conn, member_fields), table=identity.table)
            if fingerprint is None:
                continue
            sealed = seal(key, opaque, {**member_fields, "fingerprint": fingerprint,
                                        "entity_dependencies": [entry["entity_dependencies"][key] for key in sorted(entry["entity_dependencies"])],
                                        "entity_context_revision": self.resolver.entity_boundary(conn).check(
                                            table=identity.table, record_id=identity.record_id, source_id=identity.source_id,
                                            dataset_id=identity.dataset_id, row=row),
                                        "lineage": _lineage_fingerprint(conn, member_fields, row.get("content"))})
            built.append((Member(opaque, event_us, len(tokens), terms, sealed), opaque, identity, vectors))
        built.sort(key=lambda item: item[1])
        return built

    # -- interest records (IF-5 §1.3, §5; Q&A I7) ----------------------------

    def _interest_entries(self, conn, policy, now, boundary, opt_outs) -> dict:
        """The interest members a build of this grant admits on `conn`'s snapshot, as `_members` takes entries.

        `interest_index.members` makes every check: the node flag; the grant's kind, table and source; the
        visit and label checks (threshold, private windows, NSFW, exclusions, provenance, host, title, person,
        Off-limits over the label and the month's visits); the month inside the window, the current month only
        under day-level time (I1); a current, releasable label assessment; the grant's rules. Nothing is added
        or relaxed here. Each entry also has the sealed member's identity fields, so it can be checked as one.
        A check that cannot be decided (unreadable exclusions, an oversized protected vocabulary) withholds
        every interest and leaves the other families' members standing, as one undecidable message does.
        """
        from . import interest_index
        try:
            items = interest_index.members(conn, owner_id=self.resolver.binding.owner_id, policy=policy, now=now,
                                           boundary=boundary, opt_outs=opt_outs)
        except PolicyError:
            return {}
        out = {}
        for item in items:
            identity = DerivedIdentity(item["table"], item["source_id"], item["dataset_id"], item["record_id"])
            out["interest:" + item["record_id"]] = {
                "table": identity.table, "source_id": identity.source_id, "dataset_id": identity.dataset_id,
                "record_id": identity.record_id, "identity": identity, "row": {}, "facts": set(),
                "entity_dependencies": {}, "rank_text": item["rank_text"], "rank_event_us": item["rank_event_us"],
                "interest": item["interest"]}
        return out

    def _interests_current(self, conn, sealed_members, policy, boundary, opt_outs=frozenset()) -> bool:
        """Whether every interest member is still the member its build admitted (`interest_index.indexed_current`).

        With `policy`, the grant's decision is made again as well; without it the index basis pins the policy."""
        from . import interest_index
        sealed_members = list(sealed_members)
        found = interest_index.indexed_current(conn, sealed_members, owner_id=self.resolver.binding.owner_id,
                                               boundary=boundary, opt_outs=opt_outs, policy=policy)
        return found == frozenset(member["record_id"] for member in sealed_members)

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
        except Exception as exc:  # noqa: BLE001
            # Owner hooks and the daemon: if the check cannot run, no index survives it. A recipient
            # request instead refuses, so one caller's transient error never empties other grants.
            # Owner-local operations diagnosis only. Never log an exception message,
            # query, grant identifier, key, record, or evidence contents.
            _log.warning("message search index sweep unavailable (%s)", type(exc).__name__)
            if on_error == "raise":
                raise PolicyError("search_index_sweep_unavailable") from None
            removed += purge_all(self.root)
        return removed

    def _entity_dependencies_current(self, conn, boundary, dependencies, checked, provenance=None, laps=None):
        """Every support contributor, including leaves other than the ranked member.

        `provenance`: a search pass's ExistingProvenancePass (N3c), which proves recovered iMessage rows with
        one service; the caller must `finish` it after the pass's last member before a True counts.
        `laps` (timing only, IF-3 v1.4) accumulates the seconds spent in the dependencies' boundary checks.
        """
        if not isinstance(dependencies, list) or (boundary.active and not dependencies):
            return False
        for dependency in dependencies:
            identity = self.resolver._identity(dependency["table"], dependency["record_id"],
                                              dependency["source_id"], dependency["dataset_id"])
            key = _key(identity)
            if key not in checked:
                row = self.resolver._load(conn, identity, provenance=provenance)
                revision = _row_revision(row, table=identity.table)
                lap = time.perf_counter()
                context = boundary.check(table=identity.table, record_id=identity.record_id,
                                         source_id=identity.source_id, dataset_id=identity.dataset_id, row=row)
                if laps is not None:
                    laps["dependency_boundary"] = laps.get("dependency_boundary", 0.0) + time.perf_counter() - lap
                checked[key] = (revision, context)
            if checked[key] != (dependency["revision"], dependency["context"]):
                return False
        return True

    def _current(self, path, grant_id, authority, clock, conn, *, deep: bool = True, digest_point: str | None = None,
                 verified: SearchVerification | None = None, before=None, laps: dict | None = None,
                 provenance_point: str | None = None, members: bool = True) -> bool:
        """Whether the grant's index still describes R(g) on `conn`'s snapshot.

        With `verified` (a search's own stages), the boundary's closure and the review digest come
        from it: reused only when nothing they read has changed, else computed as below. `before`
        is its canonical token read before `conn`'s snapshot was established. With `verified` the
        pass also proves recovered iMessage dependencies with one provenance service of its own and
        one store check after its last member (N3c, `ExistingProvenancePass`); nothing of that
        outlives the pass. `laps` (timing only, IF-3 v1.3, parts of `members` in v1.4) receives the
        seconds spent on the boundary, the review digest and the per-member checks, and
        `provenance_point` names the pass's gate waits; neither changes the answer. `members=False` (N5, the
        index load only) stops after the basis and the key: the gated recheck runs the member loop on the
        snapshot that decides.
        """
        def stale(stage):
            _log.warning("message search index stale (%s)", stage)
            return False

        conn.row_factory = sqlite3.Row
        if grant_id is None or authority is None or authority.capability_version not in SEARCH_CAPABILITIES:
            return stale("authority")
        try:
            index = self._open(path)
        except PolicyError:
            return stale("index_integrity")
        lap = time.perf_counter()
        if verified is None:
            from .entity_boundary import EntityBoundary
            boundary = EntityBoundary(conn)
        else:
            boundary = verified.boundary(conn, before=before)
        expected = basis_of(authority, clock=clock, boundary_revision=boundary.revision)
        if laps is not None:
            laps["boundary"] = time.perf_counter() - lap
            lap = time.perf_counter()
        if authority.capability_version in DIRECT_SEARCH_CAPABILITIES:
            if verified is None:
                from . import search_timing
                with search_timing.gate_wait(digest_point):  # the digest enters the gate (evidence.py `_db`)
                    expected["message_review_revision"] = self.reviews.current_authority_digest()
            else:
                expected["message_review_revision"] = verified.digest(point=digest_point)
            if laps is not None:
                laps["digest"] = time.perf_counter() - lap
        if authority.capability_version == CAPABILITY_KNOWLEDGE_SEARCH:
            from .automatic_message_review import rubric_revision, MODEL_REVISION
            expected['automatic_rubric_revision']=rubric_revision()
            expected.update(_family_rubric_basis())
            expected['automatic_model_revision']=MODEL_REVISION
        basis = dict(index["basis"])
        if {k: v for k, v in basis.items() if k != "protection_revision"} != \
                {k: v for k, v in expected.items() if k != "protection_revision"}:
            return stale("basis")
        key = self.keys.get(grant_id, create=False)
        if key is None:
            return stale("key_missing")
        if not members:
            return True
        lap = time.perf_counter()
        provenance = None
        if verified is not None:
            from .reconciliation_provenance import ExistingProvenancePass
            provenance = ExistingProvenancePass(conn, canonical_database=self.resolver.path, binding=self.resolver.binding,
                                                gate_wait=_provenance_gate_wait(provenance_point))
        try:
            return self._members_current(index, key, conn, boundary, authority, deep, stale, provenance=provenance,
                                         laps=laps)
        finally:
            if provenance is not None:
                provenance.close()
            if laps is not None:
                laps["members"] = time.perf_counter() - lap
                if provenance is not None:
                    laps.update({f"provenance_{part}": seconds for part, seconds in provenance.seconds.items()})

    def _members_current(self, index, key, conn, boundary, authority, deep, stale, provenance=None, laps=None) -> bool:
        """`_current`'s per-member half: every sealed member re-checked against `conn`'s snapshot.

        With `provenance`, the pass's store check and snapshot re-hash run once, after the last member.
        """
        checked = {}
        interests = []
        if laps is not None:
            laps.update(dependencies=0.0, dependency_boundary=0.0)
        for opaque, sealed in index["sealed"]:
            try:
                member = unseal(key, opaque, sealed)
                if member.get("table") == "activity_events":   # an IF-5 interest: decided below, all at once
                    interests.append(member)
                    continue
                lap = time.perf_counter()
                try:
                    current = self._entity_dependencies_current(conn, boundary, member.get("entity_dependencies"), checked,
                                                                provenance=provenance, laps=laps)
                finally:
                    if laps is not None:
                        laps["dependencies"] = laps.get("dependencies", 0.0) + time.perf_counter() - lap
                if not current:
                    return stale("dependencies")
                # Deleted, scrubbed, edited, re-flagged or superseded since the build: the index no
                # longer describes R(g), so it goes (the owner's next rebuild restores search).
                rows, facts = _live_rows(conn, member)
                if authority.capability_version == CAPABILITY_KNOWLEDGE_SEARCH:
                    from .automatic_message_review import context_for
                    from .evidence import EvidenceIdentity
                    if len(rows) != 1 or context_for(conn, EvidenceIdentity.parse(member["message"]), dict(rows[0]), boundary=boundary)[0] != member.get("review_context_revision"):
                        return stale("classification_context")
                    if member.get('projection'):
                        from .knowledge_projections import current_revision
                        projection=member['projection']
                        if current_revision(conn,projection['table'],projection['record_id'])!=projection['revision']:
                            return stale('projection')
                        for context in member.get('classification_contexts',[]):
                            identity=EvidenceIdentity.parse(context['identity'])
                            if context_for(conn,identity,self.resolver._load(conn,identity,provenance=provenance),boundary=boundary)[0]!=context['revision']:
                                return stale('projection_context')
                if len(rows) != 1 or boundary.check(table=member["table"], record_id=member["record_id"],
                        source_id=member["source_id"], dataset_id=member["dataset_id"], row=dict(rows[0])) != member.get("entity_context_revision"):
                    return stale("context")
                if _member_fingerprint(rows, facts, table=member["table"]) != member["fingerprint"]:
                    return stale("fingerprint")
                if deep and _lineage_fingerprint(conn, member, dict(rows[0]).get("content")) != member["lineage"]:
                    return stale("lineage")
            except (PolicyError, sqlite3.Error, KeyError):
                return stale("member_unavailable")
        if provenance is not None:
            # After the last member, never before it: a revocation committed during this pass is visible
            # only to this check (ExistingProvenancePass). Its refusal is the pass's, as any member's is.
            try:
                provenance.finish()
            except PolicyError:
                return stale("member_unavailable")
        # IF-5 Q&A I7: every interest member is still the one its build admitted, decided at that build's instant
        # (one build per instant). The policy and the owner's opt-outs (in the review digest) are pinned by the
        # basis checked above. Like lineage, this runs on the deep (daemon and owner) sweeps only, never on a
        # recipient's request: there `_accept` decides every interest again at the read's own clock
        # (`interest_index.release_object`), so a member that changed since the build never releases, and the drift
        # is dropped within one sweep (test_interest_door pins both).
        try:
            if deep and interests and not self._interests_current(conn, interests, None, boundary):
                return stale("interest")
        except (PolicyError, sqlite3.Error, KeyError):
            return stale("member_unavailable")
        return True

    def check_own(self, grant_id: str, authority, *, now: int, digest_point: str | None = None,
                  verified: SearchVerification | None = None, laps: dict | None = None,
                  provenance_point: str | None = None, members: bool = True) -> None:
        """The request path's check: this grant's file only, O(|R(g)|). Refuses; never purges others.

        ``members=False`` (N5, index load): the basis, key and index integrity, O(1) in the members.

        ``digest_point`` names the timing line of the review digest's gate wait (search_timing.gate_wait),
        ``provenance_point`` those of the provenance pass's two gate entries (IF-3 v1.4).
        ``verified`` is the search's own SearchVerification, shared by its stages.
        """
        path = index_path(self.root, grant_id)
        try:
            before = verified.canonical_token() if verified is not None else None  # before the snapshot below
            conn = sqlite3.connect(self.resolver.path.as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("BEGIN")
                current = path.exists() and self._current(path, grant_id, authority, clock_state(conn), conn, deep=False,
                                                          digest_point=digest_point, verified=verified, before=before,
                                                          laps=laps, provenance_point=provenance_point,
                                                          members=members)
            finally:
                conn.close()
        except (sqlite3.Error, PolicyError):
            raise PolicyError("search_index_unavailable") from None
        if not current:
            if path.exists():
                with with_db_write():
                    _shred(path)
            raise PolicyError("search_index_stale")

    def send_token(self, grant_id: str, verified: SearchVerification, ledger_path) -> dict | None:
        """N5: every store the send check's `check_own` depends on, as it stands now; None when a part is unreadable.

        The canonical database and the review store as N3a reads them (data_version and file state); the identity
        and mode of `permissions-v2` and `ingest-snapshots`, not following a link (the provenance pass refuses a
        non-private or linked directory there); the ingest marker (a revocation publishes it before its commit)
        and the native snapshot directory, file by file; this grant's index file; this grant's record-id key
        (rebuild, shred, rotation); and this grant's ledger rows with the node-wide ones (revoke, pause, policy,
        epoch, protection), which the send check's authority read covers as well. The key and ledger parts are
        this grant's own, so another grant's activity never moves the token. And the evidence families that
        exist on this node now: a family behind its flag (the journal, IF-5) is read by the full check, in its
        basis and in its member loop, so a flag switched since the recheck forces it. None never matches, so the
        send check then runs in full.
        """
        try:
            from . import interest_index
            from .evidence_families import enabled_tables
            canonical, reviews = verified.canonical_token(), verified.review_token()
            if canonical is None or reviews is None:
                return None
            base = Path(self.resolver.path).parent / "permissions-v2"
            snapshots = base / "ingest-snapshots"
            listing = (tuple(sorted((entry.name, _file_state(entry)) for entry in snapshots.iterdir()))
                       if snapshots.is_dir() else None)
            ledger = Path(ledger_path)
            # `_snapshot` refuses a non-private or linked `permissions-v2` or `ingest-snapshots` (lstat); the other
            # parts follow links and hold neither directory's own mode (N5 review, R1).
            directories = (_lstat_state(base), _lstat_state(snapshots))
            return {"canonical": canonical, "reviews": reviews, "directories": directories,
                    # Process-local, like the transport's own flags, but read by `check_own` itself: a journal
                    # member's live row and the basis's family rubric exist only while the family is on (IF-5).
                    # The interest family (IF-5 I7) is not an evidence family, but the full check reads its flag in the
                    # basis (`_family_rubric_basis`) all the same, so its table joins while the flag is on.
                    "families": enabled_tables() + ((interest_index.TABLE,) if interest_index.enabled() else ()),
                    "marker": _file_state(base / "ingest-snapshots.enrollment.json"),
                    "snapshots": (_file_state(snapshots), listing),
                    "index": _file_state(index_path(self.root, grant_id)),
                    # Narrowed to this grant (WS0, after the review): another grant's key or ledger activity
                    # must not move it. Each store's own file identity (a swap for a link: `private_file` opens
                    # keys.db O_NOFOLLOW), then this grant's rows: its key's digest, never the key; the ledger
                    # rows `_authority` reads for it, plus the node-wide ones. The send check compares the
                    # authority anyway.
                    "keys": (_lstat_state(self.keys.path), hashlib.sha256(repr(verified._rows(self.keys.path, (
                        ("SELECT key FROM p2c_record_keys WHERE grant_id=?", (grant_id,)),))).encode()).hexdigest()),
                    "ledger": (_lstat_state(ledger), hashlib.sha256(repr(verified._rows(ledger, (
                        ("SELECT * FROM p2a_grants WHERE grant_id=?", (grant_id,)),
                        ("SELECT * FROM p2a_policies WHERE version_id=(SELECT version_id FROM p2a_grants WHERE grant_id=?)",
                         (grant_id,)),
                        ("SELECT * FROM p2a_grant_bindings WHERE grant_id=?", (grant_id,)),
                        ("SELECT * FROM p2a_grant_authorities WHERE grant_id=?", (grant_id,)),
                        ("SELECT * FROM p2a_node", ()),
                        ("SELECT * FROM p2a_protection_observation", ()),
                        ("SELECT * FROM p2a_canonical_floor", ())))).encode()).hexdigest())}
        except Exception:  # noqa: BLE001 -- unreadable: never matches, the send check runs in full
            return None

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
