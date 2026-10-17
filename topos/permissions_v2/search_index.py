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

MANY SHARES (N3): no owner hook builds under the gate any more. An owner change queues its index work
(index_rebuilds.py): a grant mutation rebuilds that grant only, a review change every grant one at a time, and the
indexes whose basis cannot see a review change (p2c-v1) are dropped in the change's own critical section
(`drop_unguarded`). The owner-change queue and the refresh loop's restore build under `BUILD_SLOT`, one build at a
time. A change of only the fields no build reads re-stamps the basis instead (`restamp`, A2A-4 Q5). The request path's
guard is unchanged.

NARROW MEMBERS (NR188, DR-31; `TOPOS_PERMISSIONS_V2_INDEX_NARROW_MEMBERS`, on by default): new data never takes an
index dark. A member that fails only at its `dependencies` or its `classification_context`, as a clean revision
mismatch, is withheld (`_members_current`); the sweep publishes the index without it (`_narrow`) and the request path
refuses, without shredding, until it has. Every other failure still deletes the whole index, as above.
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
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from topos.disclosure.content_policy import is_record_nsfw
from topos.principal import OWNER_APP, current_principal
from topos.storage.db.write_gate import with_db_write
from topos.storage.share_excluded import row_share_excluded

from .canonical import PolicyError, canonical_bytes, parse_json
from .evidence import _copy_count, _key, _row_revision
from .fact_eligibility import canonical_utc_microseconds
from .identity import ATTESTED_CONTRACT
from .opaque_ids import RecordKeys, opaque_record_id, private_directory, private_file, seal_key
from .protection_clock import clock_state
from .search_contract import (CAPABILITY_MESSAGE_SEARCH, RELEASABLE_SEARCH_CAPABILITIES, CAPABILITY_SEARCH,
    CAPABILITY_KNOWLEDGE_SEARCH, DIRECT_SEARCH_CAPABILITIES)

FORMAT = "topos-p2c-index/v1"
ROOT_NAME = "message-search"
# N3: the node's two automatic rebuilders (index_rebuilds.IndexRebuilds after an owner change, and the refresh loop's
# restore) take this around each build, so they never build at once and a build that starts after another publishes
# after it: a build begun before a grant changed can never overwrite the build that follows the change. Never taken by
# a caller that holds the write gate (a build takes the gate inside it).
BUILD_SLOT = threading.Lock()
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
        if candidate is path and path.name.startswith("grant-"):
            from . import search_timing
            search_timing.index_event("index_removed", path)   # attribution only (NR166): a share lost its index
        try:
            with open(candidate, "r+b", buffering=0) as handle:
                handle.write(b"\0" * size)
                os.fsync(handle.fileno())
        finally:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


#: R-N154 L5: the build files this process is writing now (absolute paths). One process owns an index root (the
#: runtime's single-process lock), so any other `.build-*.db` there is left over from a run that died mid-build.
_BUILDING: set[str] = set()
_BUILDING_LOCK = threading.Lock()


def _build_file(root: Path) -> Path:
    """A new private build file under `root`, registered as being written until `_built` is called."""
    temporary = Path(root) / (".build-" + os.urandom(8).hex() + ".db")
    with _BUILDING_LOCK:
        _BUILDING.add(str(temporary))
    private_file(temporary)
    return temporary


def _built(temporary: Path) -> None:
    with _BUILDING_LOCK:
        _BUILDING.discard(str(temporary))


def shred_orphan_builds(root: Path) -> int:
    """Shred every `.build-*.db` under `root` that no build in this process is writing: a forced exit or a crash
    mid-build left it, with its members' term bags in plain text, and nothing else ever removes it (the sweep looks
    at `grant-*.db` only). Never raises."""
    removed = 0
    try:
        candidates = sorted(Path(root).glob(".build-*.db"))
    except OSError:
        return 0
    for candidate in candidates:
        with _BUILDING_LOCK:
            writing = str(candidate) in _BUILDING
        if writing:
            continue
        try:
            _shred(candidate)
            removed += 1
        except OSError:
            pass
    if removed:
        _log.info("shredded %d leftover index build file(s)", removed)
    return removed


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


def drop_unguarded(ledger, root, *, now: int) -> int:
    """An owner review change: shred, now, every index whose basis cannot see one (N3). Returns how many went.

    A direct grant's basis (p2c-v2, p2c-v3) binds the review digest, which every review change moves, so the request
    path refuses its index until a rebuild publishes a new one. A p2c-v1 basis binds no review state: its index would
    stay valid by the guard while the permitted set it describes changed, and a search would rank on it until its
    rebuild ran (each candidate is still decided again at release). The owner hooks used to rebuild every index under
    the gate, so no search could see one; the rebuild now runs after the gate is released (index_rebuilds.py), so such
    an index goes first, inside the change's own critical section. The caller holds the write gate. An inactive grant's
    index is left to the key cleanup and the sweep, which already refuse and remove it.
    """
    root = Path(root)
    doomed = []
    with ledger._transaction() as db:
        for row in db.execute("SELECT grant_id FROM p2a_grants").fetchall():
            try:
                authority, _policy = ledger._authority(db, row["grant_id"], now)
            except PolicyError:
                continue
            if authority.capability_version == CAPABILITY_SEARCH and index_path(root, row["grant_id"]).exists():
                doomed.append(row["grant_id"])
    for grant_id in doomed:
        purge(root, grant_id)
    return len(doomed)


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


# A2A-4 Q5 (N3): the policy fields no index build reads. Two versions of one share's policy that differ only in these
# describe the same permitted set, so a change of only these re-stamps the index basis instead of rebuilding
# (`SearchIndexService.restamp`). `answers` is A2A-4's mode, inside the search declaration once the grammar carries it.
LIGHT_POLICY_FIELDS = frozenset({"policy_version_id", "read_budget_per_day"})
LIGHT_SEARCH_FIELDS = frozenset({"answers"})


def light_change(old: dict, new: dict) -> bool:
    """Whether two versions of one share's policy (model dumps) differ only in fields no index build reads."""
    def heavy(policy: dict) -> dict:
        kept = {key: value for key, value in policy.items() if key not in LIGHT_POLICY_FIELDS}
        if isinstance(kept.get("search"), dict):
            kept["search"] = {key: value for key, value in kept["search"].items() if key not in LIGHT_SEARCH_FIELDS}
        return kept
    return heavy(old) == heavy(new)


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
    if table == "ai_chat_messages":
        # NR189 (D-17): a row the owner keeps from every share is no row here, so a member flagged after its build is
        # dropped as a deleted one is (DR-31). Read off the selected row: no probe per member.
        rows = [row for row in rows if not row_share_excluded(row)]
    facts = [conn.execute("SELECT * FROM signal_objects WHERE object_id=?", (fact_id,)).fetchmany(2)
             for fact_id in member["facts"]]
    return rows, facts


def _family_rubric_basis() -> dict:
    """The assessment revisions of the families beyond messages, when they exist (IF-5 §5).

    Empty with every such family off, so a messages-only index's basis is byte for byte what it was,
    and a journal-only one keeps the bytes it had before interests. A journal rubric or floor change,
    or an interest label rubric change, moves this, and the index is rebuilt; so does turning a
    family's flag on or off. With the derived-facts flag on (IF-6 §5, which needs the journal family),
    the inferred-fact guards' version is one more revision members are judged under: a flag flip or a
    guard change drops every index (`stale("basis")`) and the restore rebuilds it. Absent with the flag
    off, so such an index keeps today's bytes.
    """
    from . import inferred_facts, interest_index, interest_review
    from .automatic_message_review import rubric_revision_for
    from .evidence_families import family
    revisions = {}
    if family("journal_entries").enabled():
        revisions["journal_entry"] = rubric_revision_for("journal_entries")
    if interest_index.enabled():
        revisions["interest"] = interest_review.rubric_revision()
    if inferred_facts.enabled():
        revisions["inferred_facts"] = inferred_facts.VERSION
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
    copies = sum(_copy_count(conn, table, content)
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


def _reuse(path: str, hit: bool, kept=None, now=None, **how) -> None:
    """Attribution only (search_timing, NR166 R0; off by default): one fast path's outcome. It decides nothing."""
    from . import search_timing
    search_timing.reuse(path, hit, kept, now, **how)


def _narrow_members() -> bool:
    """NR188 (DR-31; on by default): whether a member moved only by new data is withheld alone (`_members_current`)
    instead of its whole index going."""
    from . import switches
    return switches.on(switches.INDEX_NARROW_MEMBERS)


#: NR188 (review #3): a narrowed copy carries this table, one row, nothing else: the index it holds withholds members
#: moved by new data and owes its share one rebuild. A build writes a fresh file without it; a restamp copies it. The
#: request path never reads it (`_open` and `load` read meta, members and vectors only), so it changes no basis.
_NARROWED_DDL = "CREATE TABLE narrowed (singleton INTEGER PRIMARY KEY CHECK(singleton=1))"
#: NR188 (review #3): sweeps in a row a busy BUILD_SLOT may hold a narrowed publish back before it is made under the
#: write gate alone (`_narrow`), so a long build of another share cannot keep this one refused for long.
NARROW_SLOT_WAITS = 3


def _narrowed_file(path) -> bool:
    """NR188 (review #3): whether the index file at `path` is a narrowed copy that still owes its share a rebuild."""
    try:
        conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='narrowed'").fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _elapsed_since(started: float) -> float:
    """NR188 (review #4): seconds since `started` (time.monotonic), to carry a sweep's clock to the present."""
    return time.monotonic() - started


#: NR188 (review #2): the most narrowed publishes remembered per index file, each the identity it replaced and the
#: identity it published (`SearchIndexService.same_or_narrowed`). Change tokens only: stat integers.
SUCCESSORS_KEPT = 8


#: NR188: the `dependencies` words that are a clean revision mismatch, so a narrow failure with the switch on. `missing`
#: (no dependencies sealed where the boundary needs them) is not one: it drops the whole index, as every other class.
NARROW_DEPENDENCY_WORDS = frozenset({"row_revision", "row_binding", "conversation_context"})


def _context_rows(conn, identity, row, exclude: frozenset) -> list:
    """Attribution only (NR188): the neighbouring rows `automatic_message_review.context_for` reads as this item's
    context, [message_id, content, event_at] in order, as they would be without the rows in `exclude`. The same two
    bounded reads; `context_for` itself is not changed (the grant census pins it)."""
    from .automatic_message_review import CONTEXT_VERSIONS
    table = identity.table
    if table not in CONTEXT_VERSIONS:
        return []
    conversation, event = row.get("conversation_id"), row.get("event_at")
    if not conversation or not isinstance(event, str):
        return []
    scope, args = "conversation_id=? AND source_id=?", [conversation, identity.source_id]
    if table == "conversation_messages":
        scope += " AND dataset_id=?"
        args.append(identity.dataset_id)
    else:  # ai_chat_messages: the owner's turns, as context_for reads them
        scope += " AND sender_type IN ('human','user')"
    if exclude:
        scope += " AND message_id NOT IN (" + ",".join("?" for _ in exclude) + ")"
        args.extend(sorted(exclude))
    before = conn.execute(f"SELECT message_id,content,event_at FROM {table} WHERE {scope} "
        "AND (event_at,message_id)<(?,?) ORDER BY event_at DESC,message_id DESC LIMIT 2",
        (*args, event, identity.record_id)).fetchall()
    after = conn.execute(f"SELECT message_id,content,event_at FROM {table} WHERE {scope} "
        "AND (event_at,message_id)>(?,?) ORDER BY event_at,message_id LIMIT 2",
        (*args, event, identity.record_id)).fetchall()
    return [list(r) for r in [*reversed(before), *after]]


def _context_revision(conn, identity, row, boundary, exclude: frozenset) -> str:
    """Attribution only (NR188): `context_for`'s revision of this item's context, as it would be without the rows in
    `exclude`. With nothing excluded it is `context_for`'s own revision (test_nr188_narrow_member_drop pins that, so a
    change there is caught). Decides nothing."""
    from .automatic_message_review import CONTEXT_VERSIONS, _bound_terms
    from .canonical import digest
    context = _context_rows(conn, identity, row, exclude)
    terms = sorted(boundary.terms | boundary.handles)
    return digest({"version": CONTEXT_VERSIONS[identity.table], "context": context,
                   "protected_terms": _bound_terms(boundary, identity, row, [r[1] for r in context], terms, False)})


def _context_drift(conn, identity, row, boundary, sealed) -> str:
    """Attribution only (NR188): the fixed word for why a member's sealed classification context no longer matches.

    `neighbour_inserted` when leaving out some of the rows now next to it gives the sealed revision back: rows were
    added beside it (what a sync does), and nothing else moved. `neighbour_changed` for anything else (a neighbour's
    text or time changed, a neighbour went, the protected words that reach it moved), or when it cannot be told. At
    most 15 tries (the window holds at most four rows), run only for the first such failure of a check. Never raises,
    decides nothing, and keeps nothing it read."""
    import itertools
    try:
        neighbours = [r[0] for r in _context_rows(conn, identity, row, frozenset())]
        for size in range(1, len(neighbours) + 1):
            for removed in itertools.combinations(neighbours, size):
                if _context_revision(conn, identity, row, boundary, frozenset(removed)) == sealed:
                    return "neighbour_inserted"
    except Exception:  # noqa: BLE001 -- attribution only: what cannot be told is a change
        pass
    return "neighbour_changed"


#: NR187: the most index files whose sweep interest verdict is kept at once. The sweep also drops the entry of every
#: index file that is gone or went stale (`_sweep`), so there are never more entries than index files.
INTEREST_KEPT_MAX = 64
#: NR187: `_current`'s default: no kept interest verdict is read or written (every caller but the daemon sweep).
_NO_INTEREST_MEMO = object()
#: NR187: the parts of a kept interest verdict's state, in order, as an attribution line names a part that moved.
_INTEREST_STATE_PARTS = ("file", "key", "canonical", "boundary", "inputs")


def _sweep_interest_verdict() -> bool:
    """NR187 (off by default): whether the daemon sweep may reuse an index's last True interest verdict."""
    from . import switches
    return switches.on(switches.SWEEP_INTEREST_VERDICT)


#: NR187 (WS0 review, Q3): the most sweeps in a row an index's interests may go without a full decision.
INTEREST_FULL_EVERY_CEILING = 360


def _interest_full_every() -> int:
    """NR187 (WS0 review, Q3): every this-many-th sweep of an index decides its interests in full whatever the tokens
    say (SWEEP_INTEREST_VERDICT_FULL_EVERY: 30 by default, at least 1, at most INTEREST_FULL_EVERY_CEILING). A bound
    on how long a verdict can be reused, should some input ever move outside every token. Unreadable: 1."""
    try:
        from . import switches
        return max(1, min(INTEREST_FULL_EVERY_CEILING, switches.number(switches.SWEEP_INTEREST_VERDICT_FULL_EVERY)))
    except Exception:  # noqa: BLE001 -- unreadable: every sweep decides in full
        return 1


def _interest_inputs_held(inputs: tuple, reads: list) -> bool:
    """NR187 (WS0 review, finding 1): whether the inputs a decision is kept under are the ones it read. They are read
    before the decision; the build reads the two switches again while it runs, so a switch flipped (and even flipped
    back) during the decision could leave a True kept under values it was not decided with. Kept only when every switch
    read during the decision (`switches.observed`) is one of the two inputs, with the value the state holds, and the
    inputs read again now are the state's. Any other switch read means an input the state does not cover: not kept."""
    from . import switches
    expected = {switches.INTEREST_SOURCES.name: inputs[0], switches.INTEREST_RELABEL.name: inputs[1]}
    return (all(name in expected and bool(value) is expected[name] for name, value in reads)
            and _interest_inputs() == inputs)


def _interest_inputs() -> tuple | None:
    """NR187: what an index's interest verdict reads besides the canonical database, the index file and its key: the
    interest family's switch, the second-label switch (`interest_family.build` reads a stored second label only while
    it is on), the label rubric revision (`interest_review.current` compares every assessment with it; the pinned
    rubric file, hash-checked) and the pinned model revision. None when one cannot be read: None never matches."""
    try:
        from . import interest_index, interest_relabel, interest_review
        return (interest_index.enabled(), interest_relabel.enabled(), interest_review.rubric_revision(),
                interest_review._model_revision())
    except Exception:  # noqa: BLE001 -- unreadable: never matches, the verdict is computed in full
        return None


@contextmanager
def _gated(holds: list | None):
    """The write gate for one brief step of the sweep; appends (acquired at, held, waited) in seconds to `holds`."""
    asked = time.monotonic()
    with with_db_write():
        acquired = time.monotonic()
        try:
            yield
        finally:
            if holds is not None:
                holds.append((acquired, time.monotonic() - acquired, acquired - asked))


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
            _reuse("n3a_boundary", True)
        else:
            from .entity_boundary import EntityBoundary
            _reuse("n3a_boundary", False, kept and kept[0], after, first=not self.computed["boundary"])
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
            _reuse("n3a_digest", True)
            return kept[1]
        from . import search_timing
        _reuse("n3a_digest", False, kept and kept[0], token, first=not self.computed["digest"])
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
        _reuse("n5_keep", same, before, after, ignore=("ledger",), absent="unreadable")

    def send_unchanged(self, token: dict | None) -> bool:
        """N5: whether the send check's own read of the state (under the gate) is the one the recheck kept."""
        kept = self._send
        unchanged = kept is not None and token is not None and token == kept
        (self.reused if unchanged else self.computed)["send"] += 1
        _reuse("n5_send", unchanged, kept, token)
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
        self._published: set[str] = set()      # index file names published since the last `take_published`
        self._published_lock = threading.Lock()
        # BL-157: index file -> (the file identity the sweep checked, the review digest it saw), for an index kept
        # serving while the node-wide review digest moved (`_current(refresh=True)`); the refresh loop takes them.
        self._refresh_needed: dict[str, tuple] = {}
        # NR188: index file -> the file identity of the narrowed index a sweep published, which owes the refresh loop
        # one rebuild (`take_narrowed`, cause `members_narrowed`). Memory only.
        self._narrowed_owed: dict[str, tuple] = {}
        # NR188 (review #2): index file -> {the file identity a narrowed publish replaced: the one it published}, so a
        # removal decided about a file still removes the narrowed copy that took its place (`same_or_narrowed`).
        self._successors: dict[str, dict] = {}
        # NR188 (review #3): index file -> the sweeps in a row a busy BUILD_SLOT held its narrowed publish back.
        self._slot_waits: dict[str, int] = {}
        self._sweep_stats = threading.local()   # per thread: the last sweep's gated steps (search_timing.timed_sweep)
        # BL-171: the last proven review-guard verdict per index file (`_reviews_current_kept`), and the probe whose
        # review token says whether the review store moved since (its own connection, so its data_version compares).
        self._guard_kept: dict[str, tuple] = {}
        self._guard_lock = threading.Lock()
        self._guard_probe: SearchVerification | None = None
        # NR187: the daemon sweep's last True interest verdict per index file (`_interests_current_kept`), behind
        # SWEEP_INTEREST_VERDICT: index file name -> the state it was proven under (change tokens only; an entry exists
        # only for True). Guarded by `_guard_lock`; read through the same probe.
        self._interest_kept: dict[str, tuple] = {}
        # NR187 (WS0 review, Q3): how many sweeps in a row each kept verdict has been reused (`_interest_full_every`).
        self._interest_reuses: dict[str, int] = {}

    def take_published(self) -> set[str]:
        """The index files this service published since the last call, whether or not they still exist.

        The refresh loop's observation (refresh_loop.RefreshLoop.observe) restores an index that was published and
        then dropped. It compares the files it sees with the ones it saw at its last observation, every 10 s, and a
        file published and dropped in between was never seen (2 Oct, live: the sweep that waited on a restore's
        publish dropped the new index a second later, and the grant stayed dark for 55 minutes)."""
        with self._published_lock:
            taken, self._published = self._published, set()
        return taken

    def take_refresh_needed(self) -> dict[str, str]:
        """BL-157: the indexes a sweep kept serving although the review digest moved, by file name, with the digest
        it saw: they serve what they hold, and owe a build for what is new. A verdict about a file replaced since the
        check is dropped. Nothing is stored: after a restart the next sweep finds the same difference again."""
        with self._published_lock:
            taken, self._refresh_needed = self._refresh_needed, {}
        return {name: revision for name, (state, revision) in taken.items()
                if state is not None and _file_state(self.root / name) == state}

    def same_or_narrowed(self, path: Path, state) -> bool:
        """NR188 (review #2): whether the index file at `path` now is the file whose identity is `state`, or a narrowed
        copy that a sweep published in its place (or a copy of that copy, up to SUCCESSORS_KEPT). A removal decided about
        a file (the sweep's, `check_own`'s, a decision's `purge_stale`) then removes that copy too, as it would have
        removed the file itself with the switch off: the copy has the file's basis and a subset of its members, so a
        stale basis is stale for both. When the member that failed was one the copy withheld, the copy may still be
        current; it is removed all the same (availability, not disclosure: the refresh loop restores it). Never True
        for a file a build or a restamp published."""
        current = _file_state(path)
        if current is None:
            return False
        if current == state:
            return True
        with self._published_lock:
            chain = dict(self._successors.get(Path(path).name, {}))
        seen, here = {state}, state
        for _ in range(SUCCESSORS_KEPT):
            here = chain.get(here)
            if here is None or here in seen:
                return False
            if here == current:
                return True
            seen.add(here)
        return False

    def take_narrowed(self) -> set[str]:
        """NR188: the index files a sweep narrowed since the last call that are still the narrowed file (a build that
        replaced one since has brought its members back). Each owes the refresh loop one rebuild, cause
        `members_narrowed`, so the withheld members return with their new context (DR-31's delay). A narrowed publish is
        never in `take_published`: it settles no restore. A restamp of a narrowed index owes the same rebuild (review
        #3): it is reported here again with its new identity, so the build a restamp's publish settles is asked for once
        more. After a restart, `narrowed_files` says which indexes still owe one."""
        with self._published_lock:
            taken, self._narrowed_owed = self._narrowed_owed, {}
        return {name for name, state in taken.items() if state is not None and _file_state(self.root / name) == state}

    def narrowed_files(self) -> set[str]:
        """NR188 (review #3): the index files here that are narrowed copies still owing their share a rebuild (the
        `narrowed` table only a narrowed copy, or a restamp of one, carries). Read by the refresh loop's first
        observation after a start, so a restart between a narrowed publish and its rebuild loses nothing."""
        try:
            return {path.name for path in self.root.glob("grant-*.db") if _narrowed_file(path)}
        except OSError:
            return set()

    def _discard_unsafe(self, grant_id: str, *, now: int) -> bool:
        """BL-157: after a refresh that could not publish (the store kept moving while it ran), keep the serving index
        only when the full current check proves it, review guard included; anything else is purged as before.
        True when it stays. An index without the guard (built before 1.5.3) never stays.

        NR188 (switch on): an index whose only failures are members moved by new data stays too. The request path
        refuses those members until the sweep withholds them, so a build that new data kept from publishing never
        takes the share dark."""
        from .index_review_guard import VERSION
        path = index_path(self.root, grant_id)
        with with_db_write():
            try:
                index = self._open(path)
                if index["basis"].get("review_guard_version") == VERSION:
                    with self.ledger._transaction() as db:
                        authority, _policy = self.ledger._authority(db, grant_id, now)
                    with self.resolver._read() as (conn, _floor):
                        if self._current(path, grant_id, authority, clock_state(conn), conn, narrowed=set()):
                            return True
            except Exception:  # noqa: BLE001 -- unreadable never proves anything
                pass
            purge(self.root, grant_id)
        return False

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
                if policy.versions.capability in RELEASABLE_SEARCH_CAPABILITIES:
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

        Before the build, the goal fields of the journal entries this grant releases are stored
        (`_store_goal_fields`), so a build never publishes an index that lacks a goal it could hold.
        """
        self._require_owner(self.resolver.binding)
        self._store_goal_fields(grant_id, now)
        return self._rebuild(grant_id, now=now)

    def _store_goal_fields(self, grant_id: str, now: int | None) -> None:
        """IF-5 Lane H1 with no owner command: the lane's model-free step for this one grant, before its build.

        Every build goes through `rebuild` (the owner's hooks, the refresh loop's restore, the assessment pass's
        refresh), so the entries a grant releases and the goals stored for them cannot drift apart: a new journal
        member, a rule change or the flag coming on reaches the index at the next build, whoever asks for it.
        Nothing runs with the goal-field flag or the journal family off; for a grant that cannot hold such a goal
        (`permitted_derivation.goal_field_grant`) only its policy is read, and no write is opened. It is the same
        pass the owner's route runs (`JournalGoalFieldPass`): safe to run again, counts and codes only. A failure
        costs the goals, never the build."""
        from . import journal_goal_field
        from .evidence_families import family
        try:
            if not journal_goal_field.enabled() or not family("journal_entries").enabled():
                return
            from .permitted_derivation import JournalGoalFieldPass
            counts = JournalGoalFieldPass(self).run(now=now, grant_id=grant_id, rebuild=False)
            if counts.get("journal_members"):
                _log.info("journal goal fields before the index build: %s", json.dumps(counts, sort_keys=True))
        except Exception as exc:  # noqa: BLE001 -- class name only; the build goes on without new goals
            _log.warning("journal goal fields not stored before the index build (%s)", type(exc).__name__)

    REBUILD_ATTEMPTS = 3

    def _rebuild(self, grant_id: str, *, now: int | None = None) -> dict:
        from . import search_timing
        started = time.perf_counter()
        result = self._rebuild_now(grant_id, now=now)
        # Attribution only (NR166): how long a share's build took and how it ended. A state and a count, as
        # `rebuild` itself returns; never an id or a reason.
        search_timing.index_event("index_rebuild", index_path(self.root, grant_id), time.perf_counter() - started,
                                  state=result.get("state"), members=result.get("member_count"))
        return result

    def _rebuild_now(self, grant_id: str, *, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else now
        with self.ledger._transaction() as db:
            try:
                authority, policy = self.ledger._authority(db, grant_id, now)
            except PolicyError:
                authority = policy = None
        if policy is None:
            self.forget(grant_id)            # revoked or expired: index gone, id key rotated
            return {"state": "removed", "member_count": 0}
        if policy.versions.capability not in RELEASABLE_SEARCH_CAPABILITIES:
            purge(self.root, grant_id)       # never rotate another capability's record-id key
            return {"state": "removed", "member_count": 0}
        key = self.keys.get(grant_id, create=True)
        from . import search_timing
        # Attribution only (NR166): the mode a policy states itself; one it leaves to the client is a request's to say.
        search_timing.share_seen(index_path(self.root, grant_id), mode=getattr(policy.search, "answers", None))
        for _attempt in range(self.REBUILD_ATTEMPTS):
            result = self._rebuild_once(grant_id, authority, policy, key, now=now)
            if result is not None:
                return result
        # The owner kept changing reviews or protection while the index was being built. BL-157: an index the full
        # current check still proves keeps serving (a failed refresh is not a restriction); any other is purged.
        self._discard_unsafe(grant_id, now=now)
        return {"state": "stale", "member_count": 0}

    def restamp(self, grant_id: str, *, now: int | None = None) -> dict | None:
        """A2A-4 Q5 (N3): keep a share's index across a change of only the fields no build reads, under a new basis.

        A share's daily number (and, once the grammar carries it, its answers mode) is signed inside its policy, so a
        change of it is a new policy version: new generations, a new policy hash, an index basis that no longer
        matches, and before N3 a full rebuild with the share dark meanwhile. No build reads those fields
        (`light_change`), so the permitted set is the same set. When the index on disk was built for an earlier
        version of this grant whose policy differs from the current one only there, a copy of it is stamped with the
        current generations and policy hash, checked by the request path's own guard
        (`_current`, basis and key) against the current authority, checked again under the gate on a fresh snapshot
        with the file and the authority unchanged, and published. The members, vectors and sealed bindings are kept
        byte for byte, and nothing is built.

        Returns the index's state and member count when it re-stamped; None when a build is needed (no index, another
        version's policy that differs in more, or anything else the basis binds has moved). Owner-only.
        """
        self._require_owner(self.resolver.binding)
        now = int(time.time()) if now is None else now
        path = index_path(self.root, grant_id)
        checked = _file_state(path)
        if checked is None:
            return None
        try:
            index = self._open(path)
        except PolicyError:
            return None
        basis = dict(index["basis"])
        with self.ledger._transaction() as db:
            try:
                authority, policy = self.ledger._authority(db, grant_id, now)
            except PolicyError:
                return None
            row = db.execute("SELECT version_id FROM p2a_policies WHERE policy_hash=?",
                             (basis.get("policy_hash"),)).fetchone()
            try:
                previous = self.ledger._policy(db, row["version_id"]) if row is not None else None
            except PolicyError:
                previous = None
        # Never across a protection revision: an index built under another one is the request path's refusal
        # (`load`), whatever the clock says, and only a build may cross it.
        if (policy.versions.capability not in RELEASABLE_SEARCH_CAPABILITIES or previous is None
                or basis.get("grant_id") != grant_id or basis.get("assignment_id") != authority.assignment_id
                or basis.get("protection_revision") != authority.protection_revision
                or basis.get("policy_hash") == authority.policy_hash
                or not light_change(previous.model_dump(), policy.model_dump())):
            return None
        stamped = {**basis, "grant_generation": authority.grant_generation,
                   "assignment_generation": authority.assignment_generation, "policy_hash": authority.policy_hash}
        temporary = _build_file(self.root)
        try:
            with open(path, "rb") as source, open(temporary, "r+b") as target:
                target.write(source.read())
                target.truncate()
            copy = sqlite3.connect(str(temporary), isolation_level=None)
            try:
                copy.execute("UPDATE meta SET basis_json=? WHERE singleton=1", (canonical_bytes(stamped).decode("ascii"),))
            finally:
                copy.close()
            with open(temporary, "rb") as handle:
                os.fsync(handle.fileno())
            if not self._stamp_current(temporary, grant_id, authority):
                _shred(temporary)
                return None
            with with_db_write():
                with self.ledger._transaction() as db:
                    try:
                        current, _policy = self.ledger._authority(db, grant_id, now)
                    except PolicyError:
                        current = None
                if (current != authority or _file_state(path) != checked
                        or not self._stamp_current(temporary, grant_id, authority)):
                    _shred(temporary)
                    return None
                os.replace(temporary, path)
                os.chmod(path, 0o600)
        except BaseException:
            _shred(temporary)
            raise
        finally:
            _built(temporary)
        narrowed = _narrowed_file(path)   # NR188 (review #3): a restamp copies the members, not the build they owe
        with self._published_lock:
            self._published.add(path.name)
            if narrowed:
                self._narrowed_owed[path.name] = _file_state(path)
        return {"state": index["state"], "member_count": index["count"]}

    def _stamp_current(self, path: Path, grant_id: str, authority) -> bool:
        """The request path's guard (basis and key) on a stamped copy, on its own read snapshot."""
        try:
            conn = sqlite3.connect(self.resolver.path.as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("BEGIN")
                return self._current(path, grant_id, authority, clock_state(conn), conn, deep=False, members=False)
            finally:
                conn.close()
        except (sqlite3.Error, PolicyError):
            return False

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

    def _unchanged(self, frozen, floor, clock, *, review_guard=None, key=None) -> bool:
        """Under the gate: nothing the build depended on moved while it ran.

        BL-157: with `review_guard` (a guarded build's basis and sealed members), a review digest that moved still
        passes when none of the built members' review bindings and not the opt-out set moved: a review of something
        outside the build cannot cancel it. The floor and the clock stay exact."""
        with with_db_write():
            with self.resolver._read() as (conn, current_floor):
                if current_floor != floor or clock_state(conn) != clock:
                    return False
            with self.reviews._db() as db:
                if self.reviews._authority_digest(db) == frozen.authority_digest:
                    return True
                return (review_guard is not None and key is not None
                        and self._reviews_current(review_guard, key, db=db))

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
            built = [] if over_cap else self._members(conn, key, grant_id, {**members, **interests}, model,
                                                     frozen=frozen if automatic else None)
        basis = basis_of(authority, clock=clock, boundary_revision=boundary_revision)
        if getattr(getattr(policy, "search", None), "selective_facts", None) is not None:
            from .selective_facts import CATALOG_DIGEST
            from . import switches
            basis["selective_facts_revision"] = [CATALOG_DIGEST, switches.on(switches.SELECTIVE_FACTS)]
        if policy.versions.capability in DIRECT_SEARCH_CAPABILITIES:
            basis["message_review_revision"] = frozen.authority_digest
        if automatic:
            from .index_review_guard import VERSION, opt_out_revision
            basis["review_guard_version"] = VERSION
            basis["opt_out_revision"] = opt_out_revision(frozen.opt_outs)
            from .automatic_message_review import rubric_revision, MODEL_REVISION
            basis['automatic_rubric_revision']=rubric_revision()
            basis.update(_family_rubric_basis())
            basis['automatic_model_revision']=MODEL_REVISION
        dims = next((len(vector) for _, _, _, vectors in built for vector in vectors), None)
        with with_db_write():
            # BL-157: a review outside this build's members does not cancel it; the over-cap build keeps the
            # whole-digest check (its complete set is what the cap counts).
            review_guard = ({"basis": basis, "sealed": [(opaque, member.sealed)
                             for member, opaque, _identity, _vectors in built]}
                            if automatic and not over_cap else None)
            if not self._unchanged(frozen, floor, clock, review_guard=review_guard, key=key):
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

    def _members(self, conn, key, grant_id, members, model, *, frozen=None):
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
            if interest is None:   # sealed below as before; read here by the review guard's keys
                member_fields["entity_dependencies"] = [entry["entity_dependencies"][name]
                                                        for name in sorted(entry["entity_dependencies"])]
            if frozen is not None:
                # BL-157: the exact reviews this member can consume, absence included, sealed inside the member. An
                # interest binds none: its label and visits are decided again at every release (interest_index).
                from .index_review_guard import bindings_for
                member_fields["review_bindings"] = bindings_for(member_fields, frozen)
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
        temporary = _build_file(self.root)
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
        finally:
            _built(temporary)
        with self._published_lock:
            self._published.add(final.name)

    # -- sweep: the index is a scrub surface --------------------------------

    def sweep(self, *, now: int | None = None, on_error: str = "purge") -> int:
        """Delete every index that no longer matches the ledger, the clock or its rows. Never raises.

        The check runs on its own read snapshot, outside the write gate. The gate is taken only for brief steps:
        reading the grants' authority (the ledger), reading the review digest (the review store), and each
        removal. A removal first re-reads, under the gate, the identity of the index file the check read, and
        removes only that file: a rebuild publishes with os.replace under the gate, so an index published while
        the check ran is left for the next sweep, never removed unchecked. A stale verdict from the check's
        snapshot still holds at the removal: what it compares only moves forward (grant generations, the
        protection clock, the review digest, the rows), and only a rebuild's new file can be current again. The
        removal also re-reads the grant's authority, so an index whose grant is active again keeps its key.

        Until 1.4.4 the whole check ran under the gate. With a 502-member grant that held the gate about 9 s
        per sweep, and every writer waited, recipients' searches included (2 Oct: 39% of the gate while the
        index existed). A recipient's own gated recheck before release is unchanged, so nothing here decides
        what a recipient receives.

        NR188 (DR-31, `TOPOS_PERMISSIONS_V2_INDEX_NARROW_MEMBERS`, on by default): an index whose only failures are
        members moved by new data (`_members_current`) is not removed. The sweep publishes it without them
        (`_narrow`): the file it checked, members removed only, under BUILD_SLOT and the gate.
        """
        holds: list = []
        self._sweep_stats.holds = holds
        return self._sweep(now=now, on_error=on_error, holds=holds)

    def _authorities(self, now: int) -> dict:
        """Index file name -> (grant id, its authority at `now`, or None when inactive), one ledger read."""
        found = {}
        with self.ledger._transaction() as db:
            for row in db.execute("SELECT grant_id FROM p2a_grants").fetchall():
                try:
                    found[index_path(self.root, row["grant_id"]).name] = (
                        row["grant_id"], self.ledger._authority(db, row["grant_id"], now)[0])
                except PolicyError:
                    found[index_path(self.root, row["grant_id"]).name] = (row["grant_id"], None)
        return found

    def _sweep(self, *, now: int | None = None, on_error: str = "purge", holds: list | None = None) -> int:
        now = int(time.time()) if now is None else now
        started = time.monotonic()   # NR188 (review #4): the narrowed publish re-reads the authority at now + elapsed
        removed = 0
        shred_orphan_builds(self.root)   # R-N154 L5: not counted; they were never published indexes
        try:
            files = sorted(self.root.glob("grant-*.db"))
        except OSError:
            files = None
        # NR187: with the switch on, the kept interest verdicts of index files that are gone (of every file, when they
        # cannot be listed) are dropped; with it off, every one is.
        keep_interests = files is not None and _sweep_interest_verdict()
        if keep_interests:
            self._forget_interest_verdicts(keep={path.name for path in files})
        elif self._interest_kept:
            self._forget_interest_verdicts()
        if not files:
            return 0
        try:
            with _gated(holds):
                authorities = self._authorities(now)
            stale = []
            narrow = []   # NR188: (path, grant id, the authority checked, the file checked, the members withheld)
            # NR187: the canonical token, read BEFORE the snapshot below is established (`_interests_current_kept`).
            memo = self._canonical_token() if keep_interests else _NO_INTEREST_MEMO
            conn = sqlite3.connect(self.resolver.path.as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("BEGIN")
                clock = clock_state(conn)
                review_digest = None
                for path in files:
                    grant_id, authority = authorities.get(path.name, (None, None))
                    if (review_digest is None and authority is not None
                            and authority.capability_version in DIRECT_SEARCH_CAPABILITIES):
                        with _gated(holds):
                            review_digest = self.reviews.current_authority_digest()
                    checked = _file_state(path)  # the file this check reads; None when it is already gone
                    withheld: set = set()        # NR188: filled only with the switch on (`_current`)
                    if checked is not None and not self._current(path, grant_id, authority, clock, conn,
                                                                 review_digest=review_digest, refresh=True,
                                                                 narrowed=withheld,
                                                                 interest_before=memo):
                        stale.append((path, grant_id, checked))
                    elif checked is not None and withheld:
                        narrow.append((path, grant_id, authority, checked, frozenset(withheld)))
            finally:
                conn.close()
            for path, grant_id, checked in stale:
                self._forget_interest_verdict(path.name)   # NR187: a stale index keeps no verdict, removed or not
                with _gated(holds):
                    if not self.same_or_narrowed(path, checked):
                        continue  # replaced by a publish, or removed, since the check read it (NR188: a narrowed
                                  # copy of it is not a publish, and goes with it)
                    active = False
                    if grant_id is not None:
                        with self.ledger._transaction() as db:
                            try:
                                self.ledger._authority(db, grant_id, now)
                                active = True
                            except PolicyError:
                                active = False
                    if grant_id is not None and not active:
                        self.forget(grant_id)
                    else:
                        _shred(path)
                    removed += 1
            for path, grant_id, authority, checked, withheld in narrow:
                self._narrow(path, grant_id, authority, checked, withheld, now=now, started=started, holds=holds)
        except Exception as exc:  # noqa: BLE001
            # Owner hooks and the daemon: if the check cannot run, no index survives it. A recipient
            # request instead refuses, so one caller's transient error never empties other grants.
            # Owner-local operations diagnosis only. Never log an exception message,
            # query, grant identifier, key, record, or evidence contents.
            _log.warning("message search index sweep unavailable (%s)", type(exc).__name__)
            self._forget_interest_verdicts()   # NR187: a sweep that could not run keeps no verdict
            if on_error == "raise":
                raise PolicyError("search_index_sweep_unavailable") from None
            with _gated(holds):
                removed += purge_all(self.root)
        return removed

    def _narrow(self, path, grant_id, authority, checked, withheld: frozenset, *, now: int, started: float | None = None,
                holds=None) -> bool:
        """NR188: publish the index the sweep checked without the members it withheld. True when it published.

        BUILD_SLOT first, in the builds' order, and never waited for: a build running now publishes after this check
        anyway, and the next sweep narrows what is left. Then, outside the write gate, a new file is written from the
        checked file alone (`_narrowed_copy`): its meta (format, basis, state, model, dimensions) and its other members
        and their vectors, byte for byte; members are removed, never added. Then, under the gate, it replaces the
        index only if the index file is still the very file the check read and the grant's authority is still the one
        it was checked under (a revocation, an expiry or a re-signed policy since: nothing is published, the copy is
        shredded, and the next sweep decides the index whole). The basis is the one checked, so the request path's
        guard decides the new file exactly as it would the old one. It is not a build: it is never reported as
        published (`take_published`), so it settles no restore, and it owes one rebuild (`take_narrowed`). If the copy
        cannot be made, the index goes, as it would have without the switch.

        Review #3: a slot held through NARROW_SLOT_WAITS sweeps in a row (a long build of another share) no longer holds
        the publish back: it is then made under the write gate alone. That is as safe: every publish of an index file
        (a build, a restamp, this) replaces it under the gate, and this one only over the very file it checked, so a
        build that publishes first is never overwritten and one that publishes after replaces the copy whole; the slot
        only spares writing a copy a running build will replace. Review #4: the authority is read again at the sweep's
        `now` carried forward by the seconds the sweep has run (`started`), so an expiry while it ran is seen."""
        from . import search_timing
        held = BUILD_SLOT.acquire(blocking=False)
        with self._published_lock:
            waited = 0 if held else self._slot_waits.get(path.name, 0) + 1
            if waited:
                self._slot_waits[path.name] = waited
            else:
                self._slot_waits.pop(path.name, None)
        if not held and waited < NARROW_SLOT_WAITS:
            return False
        if not held:
            with self._published_lock:
                self._slot_waits.pop(path.name, None)
        current_now = now if started is None else now + max(0, int(_elapsed_since(started)))
        try:
            try:
                made = self._narrowed_copy(path, checked, withheld)
            except Exception as exc:  # noqa: BLE001 -- class name only; this index goes, as without the switch
                _log.warning("message search index narrowing unavailable (%s)", type(exc).__name__)
                with _gated(holds):
                    if _file_state(path) == checked:
                        _shred(path)
                return False
            if made is None:
                return False
            temporary, remaining = made
            published = False
            try:
                with _gated(holds):
                    if _file_state(path) != checked:
                        return False      # replaced by a publish, or removed, since the check read it
                    with self.ledger._transaction() as db:
                        try:
                            again = self.ledger._authority(db, grant_id, current_now)[0]
                        except PolicyError:
                            again = None
                    if again is None or again != authority:
                        return False
                    os.replace(temporary, path)
                    os.chmod(path, 0o600)
                    published = True
                    successor = _file_state(path)
                    with self._published_lock:
                        self._narrowed_owed[path.name] = successor
                        chain = self._successors.setdefault(path.name, {})
                        chain[checked] = successor
                        while len(chain) > SUCCESSORS_KEPT:
                            chain.pop(next(iter(chain)))
            finally:
                if not published:
                    _shred(temporary)
                _built(temporary)
        finally:
            if held:
                BUILD_SLOT.release()
        # NR187 meets NR188: the kept interest verdict was proven for the file this publish replaced. Its stat identity
        # is part of the kept state, so it could never match the new file; it is dropped here all the same, so nothing
        # is kept about a file that is gone.
        self._forget_interest_verdict(path.name)
        _log.warning("message search index narrowed (%d withheld)", len(withheld))
        search_timing.index_event("index_rebuild", path, state="narrowed", members=remaining)
        return True

    def _narrowed_copy(self, path, checked, withheld: frozenset) -> tuple | None:
        """NR188 (`_narrow`, under BUILD_SLOT, outside the gate): a new private build file holding the checked index
        file without `withheld`, and how many members it holds; None when the file is no longer the one checked.

        Read from that file only, and only if its stat identity is the checked one before and after the read (any
        write or replacement moves it). Every withheld member must be one of its members and its member count what its
        meta says, or nothing is made (integrity). The copy is written whole in one transaction, as `_publish` writes
        a build, so no page of the old file, and none of a withheld member's term bag or vectors, is carried into it.
        The caller publishes it or shreds it."""
        if _file_state(path) != checked:
            return None
        source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            meta = source.execute("SELECT format, basis_json, state, model, dims, member_count FROM meta "
                                  "WHERE singleton=1").fetchone()
            rows = source.execute("SELECT opaque_id, event_at_us, doc_len, terms_json, sealed FROM members "
                                  "ORDER BY opaque_id").fetchall()
            vectors = source.execute("SELECT opaque_id, chunk_index, vector FROM vectors "
                                     "ORDER BY opaque_id, chunk_index").fetchall()
        finally:
            source.close()
        if _file_state(path) != checked:
            return None
        present = {row[0] for row in rows}
        if meta is None or meta[0] != FORMAT or len(rows) != meta[5] or not withheld or not withheld <= present:
            raise PolicyError("search_index_integrity")
        kept = [row for row in rows if row[0] not in withheld]
        temporary = _build_file(self.root)
        try:
            conn = sqlite3.connect(str(temporary), isolation_level=None)
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("BEGIN")
                for sql in _DDL:
                    conn.execute(sql)
                conn.execute(_NARROWED_DDL)
                conn.execute("INSERT INTO narrowed VALUES (1)")   # review #3: owes a rebuild, on disk, with the file
                conn.execute("INSERT INTO meta VALUES (1, ?, ?, ?, ?, ?, ?)",
                             (meta[0], meta[1], meta[2], meta[3], meta[4], len(kept)))
                conn.executemany("INSERT INTO members VALUES (?, ?, ?, ?, ?)", kept)
                conn.executemany("INSERT INTO vectors VALUES (?, ?, ?)",
                                 [vector for vector in vectors if vector[0] not in withheld])
                conn.execute("COMMIT")
            finally:
                conn.close()
            with open(temporary, "rb") as handle:
                os.fsync(handle.fileno())
        except BaseException:
            _shred(temporary)
            _built(temporary)
            raise
        return temporary, len(kept)

    def _entity_dependencies_current(self, conn, boundary, dependencies, checked, provenance=None, laps=None,
                                     why: dict | None = None, every: bool = False):
        """Every support contributor, including leaves other than the ranked member.

        `provenance`: a search pass's ExistingProvenancePass (N3c), which proves recovered iMessage rows with
        one service; the caller must `finish` it after the pass's last member before a True counts.
        `laps` (timing only, IF-3 v1.4) accumulates the seconds spent in the dependencies' boundary checks.

        NR188: `why` (a dict, attribution) receives what moved: `sub` is `missing` when no dependencies are sealed
        where the boundary needs them, and `moved` maps each dependency that moved, by its key, to `row_revision` (its
        revision moved) or `conversation_context` (only its context did). `every` (the switch on) checks every
        dependency, not only up to the first that moved, so a veto or an unreadable row in any of them still raises.
        """
        if not isinstance(dependencies, list) or (boundary.active and not dependencies):
            if why is not None:
                why["sub"] = "missing"
            return False
        current = True
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
                if why is not None:
                    why.setdefault("moved", {})[key] = ("row_revision" if checked[key][0] != dependency["revision"]
                                                        else "conversation_context")
                if not every:
                    return False
                current = False
        return current

    def _current(self, path, grant_id, authority, clock, conn, *, deep: bool = True, digest_point: str | None = None,
                 verified: SearchVerification | None = None, before=None, laps: dict | None = None,
                 provenance_point: str | None = None, members: bool = True, review_digest: str | None = None,
                 only: frozenset[str] | None = None, refresh: bool = False, review_guard: bool = True,
                 narrowed: set | None = None,
                 interest_before=_NO_INTEREST_MEMO) -> bool:
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
        snapshot that decides. `review_digest`: the one the daemon sweep read under the gate for this check, as a
        step of its own, so the check itself enters no gate for it. `only` (BL-159, the re-check of a written
        answer): the member loop runs over exactly these sealed members, each of which must be in the file; the
        basis and the key are checked as always. Never for a search, which ranks the whole index.

        BL-157 (the `basis` class): an index built with the review guard (`index_review_guard`) does not go stale
        only because the node-wide review digest moved. Then every member's sealed review bindings (its own reviews,
        its context's and dependencies', an owner correction's absence included) and the opt-out set must be exactly
        as built, or it is stale (`basis`) at once. Everything else the basis binds stays exact. `refresh` (the
        daemon sweep) notes such an index as owing a build (`take_refresh_needed`).

        BL-171: `review_guard=False` (a search's index load only, with `members=False`) leaves the guard to the gated
        recheck, as N5 left it the member loop: the bindings are sealed per member, and the recheck proves them on the
        state that decides before anything is decided. A guard verdict proven for this exact file and key is reused
        while the review store has not moved at all (`_reviews_current_kept`).

        NR188 (DR-31). A stale line about a member's `dependencies` or `classification_context` is followed by a
        second line with one fixed word saying what moved (`_members_current`); the `index_stale` event carries it as
        `sub`. `narrowed` (a set): with `TOPOS_PERMISSIONS_V2_INDEX_NARROW_MEMBERS` on, a member that failed only at
        those two classes as a clean revision mismatch is added to it instead of failing the index; True then means
        every OTHER member checked is current, and the caller must never serve or keep serving the members it holds.
        With the switch off it stays empty and the check is main's.

        NR187: `interest_before` (the daemon sweep only, with SWEEP_INTEREST_VERDICT on) is the canonical token the sweep
        read before `conn`'s snapshot was established (None when unreadable). The interest step then reuses this very
        file's last True interest verdict while nothing it reads has changed (`_interests_current_kept`). Every other
        caller, and the sweep with the switch off, decides the interests in full as before.
        """
        def stale(stage, sub=None):
            _log.warning("message search index stale (%s)", stage)
            if sub is not None:
                _log.warning("message search index member drift (%s: %s)", stage, sub)   # NR188: a fixed word
            from . import search_timing
            search_timing.index_event("index_stale", path, reason=stage,     # attribution only (NR166, NR188)
                                      **({"sub": sub} if sub is not None else {}))
            return False

        def withheld(stage, sub):
            """NR188 (switch on): the first member of this check withheld at `stage`, and why, in fixed words."""
            _log.warning("message search index member drift (%s: %s)", stage, sub)
            from . import search_timing
            search_timing.index_event("index_stale", path, reason=stage, sub=sub, scope="member")

        narrowing = narrowed is not None and _narrow_members()

        conn.row_factory = sqlite3.Row
        if grant_id is None or authority is None or authority.capability_version not in RELEASABLE_SEARCH_CAPABILITIES:
            return stale("authority")
        # BL-171 (R-N154 M1): the stat identity of the file `_open` is about to read, taken BEFORE the read. Taken
        # after it, a publish (os.replace) between the two let a check prove the guard on the file it read and keep
        # the verdict under the identity of the file that replaced it. `_reviews_current_kept` re-stats after the
        # read, and reuses or keeps only when both stats agree.
        opened_state = _file_state(path)
        try:
            index = self._open(path)
        except PolicyError:
            return stale("index_integrity")
        from . import search_timing
        search_timing.share_seen(path, members=index["count"])   # attribution only (NR166): for this share's lines
        index_state = _file_state(path) if refresh else None
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
            if review_digest is not None:
                expected["message_review_revision"] = review_digest
            elif verified is None:
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
        if "selective_facts_revision" in basis:
            from .selective_facts import CATALOG_DIGEST
            from . import switches
            expected["selective_facts_revision"] = [CATALOG_DIGEST, switches.on(switches.SELECTIVE_FACTS)]
        from .index_review_guard import VERSION as GUARD_VERSION
        guarded = (authority.capability_version == CAPABILITY_KNOWLEDGE_SEARCH
                   and basis.get("review_guard_version") == GUARD_VERSION)
        ignored = {"protection_revision"}
        if guarded:
            ignored |= {"message_review_revision", "review_guard_version", "opt_out_revision"}
        if {k: v for k, v in basis.items() if k not in ignored} != \
                {k: v for k, v in expected.items() if k not in ignored}:
            return stale("basis")
        key = self.keys.get(grant_id, create=False)
        if key is None:
            return stale("key_missing")
        review_moved = guarded and basis.get("message_review_revision") != expected.get("message_review_revision")
        if guarded and review_guard and not review_moved:
            from . import search_timing
            search_timing.fast_path("bl171_guard", "skip")  # attribution only: the guard was not needed
        if review_moved and review_guard and not self._reviews_current_kept(path, opened_state, index, key):
            return stale("basis")
        if not members:
            return True
        lap = time.perf_counter()
        # N3c gave a search's passes one provenance service each. NR166 R2: every pass has one, the daemon sweep's
        # too, which proved each member with a service of its own (three gate entries per member, each of which
        # could wait behind a release). A pass that refuses is `member_unavailable`, as one member's refusal was.
        from .reconciliation_provenance import ExistingProvenancePass
        provenance = ExistingProvenancePass(conn, canonical_database=self.resolver.path, binding=self.resolver.binding,
                                            gate_wait=_provenance_gate_wait(provenance_point))
        try:
            # NR166B: a member's row is matched for its dependencies, its review context and its entity context;
            # inside one pass each row is matched once (EntityBoundary.one_pass), and the memory ends with the loop.
            with boundary.one_pass():
                current = self._members_current(index, key, conn, boundary, authority, deep, stale,
                                                 provenance=provenance, laps=laps, only=only,
                                                 narrowed=narrowed if narrowing else None, withheld=withheld,
                                                 interest_memo=None if interest_before is _NO_INTEREST_MEMO
                                                 else (path, opened_state, interest_before))
            if current and review_moved and refresh:
                with self._published_lock:
                    self._refresh_needed[path.name] = (index_state, expected["message_review_revision"])
            return current
        finally:
            if provenance is not None:
                provenance.close()
            if laps is not None:
                laps["members"] = time.perf_counter() - lap
                if provenance is not None:
                    laps.update({f"provenance_{part}": seconds for part, seconds in provenance.seconds.items()})

    def _probe(self) -> SearchVerification:
        """This service's own long-lived probe: its connections' data_version compares across searches and sweeps."""
        with self._guard_lock:
            if self._guard_probe is None:
                self._guard_probe = SearchVerification(self.resolver, self.reviews)
            return self._guard_probe

    def _guard_token(self) -> tuple | None:
        """The review store's token (`SearchVerification.review_token`: data_version, file states, rollback floor,
        clock, incarnation) read through this service's own probe, so two reads compare across searches."""
        return self._probe().review_token()

    def _canonical_token(self) -> tuple | None:
        """NR187: the canonical database's token (`SearchVerification.canonical_token`: data_version, the main, -wal
        and -journal file states, the incarnation) read through this service's own probe, so two reads compare across
        sweeps. None when unreadable: None never matches."""
        return self._probe().canonical_token()

    def _interests_current_kept(self, conn, interests, boundary, key, path, opened_state, before) -> bool:
        """NR187: `_interests_current` for the daemon sweep, or its last True verdict for this very index file while
        nothing that verdict reads has changed.

        R12 (16 Oct 2026): the sweep runs, waits 10 s and runs again, and for every index holding interest members it
        rebuilt those members' clusters (`interest_family.build`, about 4.6 s of CPU on the owner's node) every time:
        with two shares about 35% of a core, in the process (and under the GIL) that serves the owner's own chat.

        What the verdict reads, from `interest_index.indexed_current` down as the sweep calls it (no policy, no
        opt-outs; the basis checked before this step pins both), and the part of the state that covers it:
        - `file`, `key`: the sealed interest members, i.e. the index file and the grant's record-id key. The file's stat
          identity taken before `_open` read it and again here, equal (so the members are the ones that file holds;
          BL-171, R-N154 M1), and the key's sha256;
        - `canonical`: every row the build and the assessment check read (visits, clusters and their members, mentions,
          entities, exclusions, receipts and installs, embeddings, label assessments, second labels), all on the
          sweep's one read snapshot. The canonical token read before that snapshot was established (`before`) must
          equal the one read here: then no commit landed in between, and the snapshot holds the rows the token names;
        - `boundary`: the Off-limits closure, built on that same snapshot, by its revision;
        - `inputs`: the switches and pinned revisions the build reads (`_interest_inputs`). The build reads the two
          switches again while it runs, so a verdict is kept only when every switch read during the decision is one of
          the two, with the value the state holds, and the inputs read again after it are the state's (WS0 review,
          finding 1: a switch flipped and flipped back during the build);
        - the clock: nothing. Each member is decided at its own sealed `built_at` (`indexed_current`): a visit's time,
          the open month's end and every assessment are compared with that instant, never the wall clock, and no query
          reads the time. The grant's window and rules are decided again by a recipient's release at its own clock.
        The verdict is reused only when all of these equal the state it was proven under and `before` equals the token
        read here (a token unreadable here is None: never the one read before). It is reused at most N-1 sweeps in a row:
        every Nth decides in full whatever the tokens say (`_interest_full_every`, 30 by default; WS0 review, Q3). A
        read error inside a decision does not invalidate a kept verdict: with every token unchanged it cannot be a
        committed change, so it is met at the next full decision (WS0's ruling). A part that cannot be read is None and
        never matches, and any error recomputes. A False verdict is
        never kept (the index is stale and goes), and the sweep drops the entry of an index that went stale or whose
        file is gone, so there are never more entries than index files, and never more than INTEREST_KEPT_MAX.

        The owner's rule (contracts/DISCLOSURE_RULES.md DR-19) is that nothing derived from the Off-limits list or from
        excluded entities outlives the request or pass that read it. An entry is one boolean (it exists only for True)
        and change tokens: stat integers, a key digest, a data_version integer and file states, the closure's revision
        digest (which every index basis already holds on disk), two switch booleans and two digests of pinned code.
        A boolean and those tokens derive nothing from those entities. No name, name key, label, pattern, member,
        `Build`, `Snapshot` or boundary is kept: they die with the build, as NR178 requires."""
        from . import search_timing
        if opened_state is None or _file_state(path) != opened_state:
            search_timing.fast_path("nr187_interest", "miss", "file_replaced")  # attribution only, as below
            self._forget_interest_verdict(path.name)
            return self._interests_current(conn, interests, None, boundary)
        try:
            state = (opened_state, hashlib.sha256(key).hexdigest(), self._canonical_token(), boundary.revision,
                     _interest_inputs())
        except Exception:  # noqa: BLE001 -- unreadable: computed in full and not kept
            state = None
        # Only on a snapshot still open (a read transaction that has not ended, so every read of the check is of it).
        provable = (state is not None and None not in state and before is not None and state[2] == before
                    and getattr(conn, "in_transaction", False) is True)
        with self._guard_lock:
            kept = self._interest_kept.get(path.name)
            reuses = self._interest_reuses.get(path.name, 0)
        due = reuses + 1 >= _interest_full_every()   # WS0 review Q3: every Nth sweep decides in full regardless
        if provable and kept == state and not due:
            with self._guard_lock:
                if self._interest_kept.get(path.name) == state:
                    self._interest_reuses[path.name] = reuses + 1
            _reuse("nr187_interest", True)
            return True
        if kept is None:
            search_timing.fast_path("nr187_interest", "first")
        elif state is None:
            search_timing.fast_path("nr187_interest", "miss", "unreadable")
        elif provable and kept == state:
            search_timing.fast_path("nr187_interest", "miss", "full_every")
        else:
            moved = [name for name, old, new in zip(_INTEREST_STATE_PARTS, kept, state) if old != new]
            search_timing.fast_path("nr187_interest", "miss",
                                    ":".join(moved + ([] if provable else ["before"])) or "unreadable")
        from . import switches
        with switches.observed() as reads:
            verdict = self._interests_current(conn, interests, None, boundary)
        if verdict is True and provable and _interest_inputs_held(state[4], reads):
            self._keep_interest_verdict(path.name, state)
        else:
            self._forget_interest_verdict(path.name)
        return verdict

    def _keep_interest_verdict(self, name: str, state: tuple) -> None:
        """NR187: keep one index file's proven state, never more than INTEREST_KEPT_MAX entries (oldest first out). A
        new proof starts its run of reuses at none."""
        with self._guard_lock:
            self._interest_kept.pop(name, None)
            while len(self._interest_kept) >= INTEREST_KEPT_MAX:
                self._interest_kept.pop(next(iter(self._interest_kept)))
            self._interest_kept[name] = state
            self._interest_reuses = {other: count for other, count in self._interest_reuses.items()
                                     if other in self._interest_kept and other != name}
            self._interest_reuses[name] = 0

    def _forget_interest_verdict(self, name: str) -> None:
        with self._guard_lock:
            self._interest_kept.pop(name, None)
            self._interest_reuses.pop(name, None)

    def _forget_interest_verdicts(self, *, keep=None) -> None:
        """NR187: drop every kept interest verdict, or every one but those of the index files named in `keep`."""
        with self._guard_lock:
            if keep is None:
                self._interest_kept.clear()
                self._interest_reuses.clear()
            else:
                for name in [name for name in self._interest_kept if name not in keep]:
                    del self._interest_kept[name]
                for name in [name for name in self._interest_reuses if name not in self._interest_kept]:
                    del self._interest_reuses[name]

    def _reviews_current_kept(self, path, opened_state, index, key) -> bool:
        """BL-171: `_reviews_current`, or its last True verdict for this very file and key while nothing in the review
        store has moved since it was proven.

        1.5.3 ran the guard in full at every stage of every search once the node-wide review digest had moved (the
        steady state on a node whose catch-up keeps assessing): the write gate, the store's whole authority digest
        and an unseal, review parse and digest per member, three times per search (index load, gated recheck, send
        check) and every sweep. The verdict is a function of the index file, the grant's key and the review store's
        rows (reviews and opt-outs). It is reused only when the file is the one proven (stat identity before and
        after it was opened, the same as when proven), the key is the same, and the review store's token equals the
        one read before AND after the proof; a token that cannot be read never matches. A False verdict is never
        kept: such an index is stale and goes."""
        from . import search_timing
        if opened_state is None or _file_state(path) != opened_state:
            search_timing.fast_path("bl171_guard", "miss", "file_replaced")  # attribution only, as below
            return self._reviews_current(index, key)
        identity = (opened_state, hashlib.sha256(key).hexdigest())
        token = self._guard_token()
        with self._guard_lock:
            kept = self._guard_kept.get(path.name)
        if token is not None and kept == (identity, token):
            _reuse("bl171_guard", True)
            return True
        if kept is not None and kept[0] != identity:
            search_timing.fast_path("bl171_guard", "miss", "identity")
        else:
            _reuse("bl171_guard", False, kept and kept[1], token, first=kept is None)
        verdict = self._reviews_current(index, key)
        if verdict and token is not None and self._guard_token() == token and _file_state(path) == opened_state:
            with self._guard_lock:
                self._guard_kept[path.name] = (identity, token)
        return verdict

    def _reviews_current(self, index, key, *, db=None) -> bool:
        """BL-157: the opt-out set and every member's sealed review bindings are exactly as built (point reads on
        the verified review store). A member without bindings, with a missing, extra or unreadable one, fails."""
        from .index_review_guard import current, opt_out_revision
        if db is None:
            with self.reviews._db() as review_db:
                return self._reviews_current(index, key, db=review_db)
        if opt_out_revision(self.reviews._opt_outs_in(db)) != index["basis"].get("opt_out_revision"):
            return False
        checked: dict = {}
        try:
            return all(current(unseal(key, opaque, sealed), self.reviews, db, checked)
                       for opaque, sealed in index["sealed"])
        except (PolicyError, KeyError, TypeError, ValueError):
            return False

    def _members_current(self, index, key, conn, boundary, authority, deep, stale, provenance=None, laps=None,
                         only: frozenset[str] | None = None, narrowed: set | None = None, withheld=None,
                         interest_memo: tuple | None = None) -> bool:
        """`_current`'s per-member half: every sealed member re-checked against `conn`'s snapshot.

        With `provenance`, the pass's store check and snapshot re-hash run once, after the last member. With `only`,
        exactly those members (each must be present), for the re-check of a written answer (BL-159). `interest_memo`
        (NR187, the daemon sweep with its switch on): (index path, its stat identity before it was opened, the canonical
        token read before the snapshot), for `_interests_current_kept`; None decides the interests in full.

        NR188 (DR-31). A failure at `dependencies` or `classification_context` is said with a fixed word (`stale`'s
        second argument): `dependencies` is `conversation_context` (only a dependency's conversation context moved:
        a new participant or sender), `row_binding` (the member's own row: its revision moved and its stored columns
        did not, so what the node binds onto it at load moved: its source's posture, its proof, its AI chat's row),
        `row_revision` (any other dependency revision move, the member's own stored row included) or `missing`;
        `classification_context` is `neighbour_inserted`, `neighbour_changed` (`_context_drift`) or `row_unavailable`.

        `narrowed` (a set, given by `_current` only with the switch on): a member whose ONLY failures are at
        `dependencies` (each moved dependency a clean revision mismatch, every one of them checked) and at
        `classification_context` (a clean revision mismatch) is a narrow failure: it is added to the set, and the loop
        goes on with it (its other checks must all pass) and with every other member, exactly as today. Its own
        `context` check reads the very value its own-row dependency check has just found moved (one `boundary.check`
        of the same row on the same snapshot); only that duplicate is part of the same failure (`_same_drift`). Every
        other failure fails the index as before: any exception (an `entity_protected` veto, an unreadable row:
        `member_unavailable`), `missing`, `row_unavailable`, `live_rows`, `projection`, `projection_context`, any other
        `context` move, `fingerprint`, `lineage`, `interest`, and the provenance pass's refusal. `withheld(stage, word)`
        reports the first narrow failure of each class once per check.
        """
        checked = {}
        interests = []
        said = set()   # NR188: the classes whose first narrow failure this check has reported
        if laps is not None:
            laps.update(dependencies=0.0, dependency_boundary=0.0)

        def spent(step: str, since: float) -> float:
            """Timing only (NR166 R2b's profile): one member's `step` took from `since` to now. Returns now."""
            now = time.perf_counter()
            if laps is not None:
                laps[step] = laps.get(step, 0.0) + now - since
            return now
        if only is not None and not only.issubset(opaque for opaque, _sealed in index["sealed"]):
            return stale("member_unavailable")
        for opaque, sealed in index["sealed"]:
            if only is not None and opaque not in only:
                continue
            try:
                lap = time.perf_counter()
                member = unseal(key, opaque, sealed)
                spent("unseal", lap)
                if member.get("table") == "activity_events":   # an IF-5 interest: decided below, all at once
                    interests.append(member)
                    continue
                lap = time.perf_counter()
                why: dict = {}   # NR188: what moved, for the fixed word
                try:
                    current = self._entity_dependencies_current(conn, boundary, member.get("entity_dependencies"), checked,
                                                                provenance=provenance, laps=laps, why=why,
                                                                every=narrowed is not None)
                finally:
                    if laps is not None:
                        laps["dependencies"] = laps.get("dependencies", 0.0) + time.perf_counter() - lap
                moved = False    # NR188: this member failed so far only as a narrow failure
                if not current:
                    word = self._dependency_word(conn, member, why)
                    if narrowed is None or word not in NARROW_DEPENDENCY_WORDS:
                        return stale("dependencies", word)
                    moved = True
                    if "dependencies" not in said:
                        said.add("dependencies")
                        withheld("dependencies", word)
                # Deleted, scrubbed, edited, re-flagged or superseded since the build: the index no
                # longer describes R(g), so it goes (the owner's next rebuild restores search).
                lap = time.perf_counter()
                rows, facts = _live_rows(conn, member)
                lap = spent("live_rows", lap)
                if authority.capability_version == CAPABILITY_KNOWLEDGE_SEARCH:
                    from .automatic_message_review import context_for
                    from .evidence import EvidenceIdentity
                    if len(rows) != 1:
                        return stale("classification_context", "row_unavailable")
                    identity = EvidenceIdentity.parse(member["message"])
                    if context_for(conn, identity, dict(rows[0]), boundary=boundary)[0] != member.get("review_context_revision"):
                        if narrowed is None:
                            return stale("classification_context", _context_drift(
                                conn, identity, dict(rows[0]), boundary, member.get("review_context_revision")))
                        moved = True
                        if "classification_context" not in said:
                            said.add("classification_context")
                            withheld("classification_context", _context_drift(
                                conn, identity, dict(rows[0]), boundary, member.get("review_context_revision")))
                    if member.get('projection'):
                        from .knowledge_projections import current_revision
                        projection=member['projection']
                        # The boundary: Off-limits is decided again on the projection's current rows (their
                        # volatile columns are outside the revision, not outside the scan).
                        try:
                            revision=current_revision(conn,projection['table'],projection['record_id'],boundary=boundary)
                        except PolicyError:
                            return stale('projection')
                        if revision!=projection['revision']:
                            return stale('projection')
                        for context in member.get('classification_contexts',[]):
                            identity=EvidenceIdentity.parse(context['identity'])
                            if context_for(conn,identity,self.resolver._load(conn,identity,provenance=provenance),boundary=boundary)[0]!=context['revision']:
                                return stale('projection_context')
                lap = spent("review_context", lap)
                if len(rows) != 1:
                    return stale("context")
                entity_context = boundary.check(table=member["table"], record_id=member["record_id"],
                                                source_id=member["source_id"], dataset_id=member["dataset_id"],
                                                row=dict(rows[0]))
                if entity_context != member.get("entity_context_revision") and not (
                        moved and self._same_drift(member, entity_context, why, checked)):
                    return stale("context")
                lap = spent("entity_context", lap)
                if _member_fingerprint(rows, facts, table=member["table"]) != member["fingerprint"]:
                    return stale("fingerprint")
                lap = spent("fingerprint", lap)
                if deep and _lineage_fingerprint(conn, member, dict(rows[0]).get("content")) != member["lineage"]:
                    return stale("lineage")
                if deep:
                    spent("lineage", lap)
                if moved:
                    narrowed.add(opaque)   # NR188: withheld; every other member is still checked
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
            if deep and interests and not (self._interests_current(conn, interests, None, boundary)
                                           if interest_memo is None else
                                           self._interests_current_kept(conn, interests, boundary, key, *interest_memo)):
                return stale("interest")
        except (PolicyError, sqlite3.Error, KeyError):
            return stale("member_unavailable")
        return True

    def _own_key(self, member: dict) -> str:
        """NR188: the key `_entity_dependencies_current` gives the member's own row among its dependencies."""
        return _key(self.resolver._identity(member["table"], member["record_id"], member["source_id"],
                                            member["dataset_id"]))

    def _dependency_word(self, conn, member: dict, why: dict) -> str:
        """NR188, attribution: the fixed word for a member's failed dependency check (`_members_current`). Never
        raises: what cannot be told is `row_revision`, the word that is no narrower than the others."""
        if why.get("sub") == "missing":
            return "missing"
        moved = why.get("moved") or {}
        rows = [name for name, word in moved.items() if word == "row_revision"]
        if moved and not rows:
            return "conversation_context"
        try:
            if (rows == [self._own_key(member)]
                    and _member_fingerprint(*_live_rows(conn, member), table=member["table"]) == member.get("fingerprint")):
                return "row_binding"
        except Exception:  # noqa: BLE001 -- attribution only
            pass
        return "row_revision"

    def _same_drift(self, member: dict, current_context, why: dict, checked: dict) -> bool:
        """NR188: whether a member's own `context` check found its context moved exactly as its own-row dependency
        check found that dependency's context moved. Both are one `boundary.check` of the member's own row on this
        snapshot, sealed equal at the build: then it is the dependency failure read twice, not a failure of its own.
        Anything else (no own-row dependency, sealed values that differ, a value that differs now) is not."""
        try:
            own = self._own_key(member)
        except (PolicyError, KeyError):
            return False
        sealed = next((dependency for dependency in member.get("entity_dependencies") or ()
                       if isinstance(dependency, dict)
                       and (dependency.get("table"), dependency.get("record_id"), dependency.get("source_id"),
                            dependency.get("dataset_id"))
                       == (member["table"], member["record_id"], member["source_id"], member["dataset_id"])), None)
        return (own in (why.get("moved") or {}) and sealed is not None
                and sealed.get("context") == member.get("entity_context_revision")
                and own in checked and checked[own][1] == current_context)

    def check_own(self, grant_id: str, authority, *, now: int, digest_point: str | None = None,
                  verified: SearchVerification | None = None, laps: dict | None = None,
                  provenance_point: str | None = None, members: bool = True, review_guard: bool = True,
                  released: frozenset[str] | None = None) -> None:
        """The request path's check: this grant's file only, O(|R(g)|). Refuses; never purges others.

        ``members=False`` (N5, index load): the basis, key and index integrity, O(1) in the members.
        ``review_guard=False`` (BL-171, index load): the per-member review guard is left to the gated recheck.

        ``digest_point`` names the timing line of the review digest's gate wait (search_timing.gate_wait),
        ``provenance_point`` those of the provenance pass's two gate entries (IF-3 v1.4).
        ``verified`` is the search's own SearchVerification, shared by its stages.

        NR188 (switch on). A failure of only members moved by new data refuses as not served now
        (`search_index_stale`) and never shreds: the sweep withholds them within one sweep gap. ``released`` (the
        send check: the records the checkpointed decision released): every one must still be a member of the file
        checked, or the send refuses, again without shredding, since the sweep may have withheld one after the
        checkpoint. With the switch off neither applies.
        """
        path = index_path(self.root, grant_id)
        checked_state = _file_state(path)   # BL-157: shred only the file this check read, never a newer publication
        withheld: set = set()               # NR188: filled only with the switch on (`_current`)
        try:
            before = verified.canonical_token() if verified is not None else None  # before the snapshot below
            conn = sqlite3.connect(self.resolver.path.as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("BEGIN")
                current = path.exists() and self._current(path, grant_id, authority, clock_state(conn), conn, deep=False,
                                                          digest_point=digest_point, verified=verified, before=before,
                                                          laps=laps, provenance_point=provenance_point,
                                                          members=members, review_guard=review_guard,
                                                          narrowed=withheld)
            finally:
                conn.close()
        except (sqlite3.Error, PolicyError):
            raise PolicyError("search_index_unavailable") from None
        if not current:
            if path.exists():
                with with_db_write():
                    if self.same_or_narrowed(path, checked_state):   # NR188: or the narrowed copy in its place
                        _shred(path)
            raise PolicyError("search_index_stale")
        if withheld:
            raise PolicyError("search_index_stale")   # NR188: kept for the sweep to narrow, never shredded here
        if released is not None and _narrow_members() and not self._holds(path, checked_state, released):
            raise PolicyError("search_index_stale")   # NR188: a released record withheld since the checkpoint

    def _holds(self, path: Path, checked_state, released) -> bool:
        """NR188: whether the file `check_own` checked (the same stat identity) still holds every released record."""
        try:
            index = self._open(path)
        except PolicyError:
            return False
        return (_file_state(path) == checked_state
                and set(released) <= {opaque for opaque, _sealed in index["sealed"]})

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
        basis and in its member loop, so a flag switched since the recheck forces it; the derived-facts flag (IF-6)
        too, read by the full check in the basis and in `fact_projection`'s step 7. None never matches, so the send
        check then runs in full.
        """
        try:
            from . import inferred_facts, interest_index, shadow_labeler_local
            from .evidence_families import enabled_tables
            from . import switches
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
                    # basis (`_family_rubric_basis`) all the same, so its table joins while the flag is on. So does
                    # the derived-facts flag (IF-6 §7): the basis carries the inferred-fact guards' version and step 7
                    # releases an inferred fact only while it is on (`inferred_facts.enabled`).
                    "families": enabled_tables() + ((interest_index.TABLE,) if interest_index.enabled() else ())
                                + (("inferred_facts",) if inferred_facts.enabled() else ())
                                + (("selective_facts",) if switches.on(switches.SELECTIVE_FACTS) else ()),
                    # The pinned rubric file the full check reads on every `_current` for knowledge grants
                    # (`shadow_labeler_local.rubric()`, hash-verified). Unreadable or changed since the recheck,
                    # the full check refuses (RubricMismatch); so the token must move with it (N5 review 3, F1).
                    "rubric": _file_state(shadow_labeler_local.RUBRIC_PATH),
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
