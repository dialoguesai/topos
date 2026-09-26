"""Node-local fact evidence qualification; neither a grant nor a release API.

An owner-asserted fact about the owner whose terminal sources are the owner's own
words qualifies under implicit review: the node's deterministic labels stand in
for an owner review until the owner deselects the fact (an opt-out in the private
review store) or records an explicit review, which then takes precedence. Every
integrity check -- owner-authored terminal sources, revision agreement, bounds,
protection floors, independent copies -- runs on implicit and explicit reviews
alike. Pack names, legacy confirmation flags and recipient-supplied labels confer
no authority. See EVIDENCE.md, "Implicit review".
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from typing import Literal

from pydantic import model_validator

from topos.principal import OWNER_APP, current_principal
from topos.features.provenance.roles import record_role
from topos.storage.db.migrations.permissions_read_path_indexes_v1 import CONTENT_KEY
from topos.storage.db.migrations import permissions_fact_lineage_keys_v1 as lineage_keys
from topos.storage.db.write_gate import with_db_write

from .canonical import MAX_INTEGER, PolicyError, Rows, canonical_bytes, digest, digest_stream
from .contract import Hash, Identifier, Number, StrictModel
from .identity import (ATTESTED_CONTRACT, LEGACY_CONTRACT, SUBJECT_CONTRACTS, closure_identity,
    legacy_owner_subjects, permit_subjects, rekeyed_facts, restriction_subjects)  # noqa: F401 (legacy_owner_subjects: patched by tests)
from .protection_clock import clock_state, closure_protection_revision, current_protection_revision

MAX_NODES = 128
MAX_DEPTH = 16
LEAF_TABLES = ("conversation_messages", "ai_chat_messages")
# The owner's attestation covers every column except these operational ones,
# which routine syncs, re-derivations and derived scrubs rewrite without any
# change to the reviewed content, role, time, identity or lineage. A column
# missing from this closed list is consent-relevant by default.
REVIEW_SURFACE_EXCLUSIONS = {
    "conversation_messages": frozenset({"ingested_at", "sync_batch_id", "created_at", "content_hash",
        "content_disclosure", "content_disclosure_hash", "content_disclosure_model"}),
    "ai_chat_messages": frozenset({"ingested_at", "sync_batch_id", "content_hash", "content_disclosure",
        "content_disclosure_hash", "content_rendered_disclosure", "content_rendered_disclosure_hash", "content_disclosure_model"}),
    "ai_chat_conversations": frozenset({"ingested_at", "sync_batch_id", "created_at", "updated_at"}),
    "signal_objects": frozenset({"created_at", "updated_at", "created_by", "updated_by", "confidence"}),
}
_ANY_REVIEW = object()
# A fact's own disclosure never withholds the owner's own words under the owner's own policy:
# `owner_only` is what the extractor writes for every owner-asserted fact, `scoped` what it writes
# for facts asserted by others. Missing, unknown and any other value still withhold.
SHAREABLE_DISCLOSURES = ("scoped", "owner_only")
IMPLICIT_REVIEW_PREFIX = "implicit:"
# owner-review-vocabulary/v1 labels a fact carries under implicit review, keyed by the fact's
# predicate (facts.store.KNOWN_PREDICATES) and, as the fallback key, its signal dimension. The
# pairing errs towards the more protective sensitivity: a policy releases a fact only when its
# domains intersect these and its sensitivities include this one, so a wrong label can hide a
# fact from a policy but cannot hand a health or home claim to a work-only one. Anything unkeyed is
# labelled with the most protective pairing so that only a policy naming it can release it.
IMPLICIT_LABELS = {
    "works_at": (("work",), "none"), "worked_at": (("work",), "none"), "works_on": (("work",), "none"),
    "role_is": (("work",), "none"), "certified_in": (("work",), "none"), "studied_at": (("work",), "none"),
    "skilled_in": (("work",), "none"),
    "prefers": (("hobbies",), "personal"), "member_of": (("relationships",), "personal"),
    "lives_in": (("home",), "personal"),
    "practices": (("health",), "special"), "training_for": (("health",), "special"),
    "work": (("work",), "none"), "preferences": (("hobbies",), "personal"), "places": (("home",), "personal"),
    "wellbeing": (("health",), "special"),
}
IMPLICIT_FALLBACK = (("relationships",), "special")
_IDENTITY_SELECT = "SELECT binding_json,file_revision,clock_id,highest_generation,store_id FROM review_identity WHERE singleton=1"
_STORE_ID = re.compile(r"[0-9a-f]{64}")
# Never ORDER BY rowid: `fact_reviews` is a rowid table and VACUUM renumbers rowids,
# which would turn a benign physical reorder into a rollback refusal.
# `ORDER BY review_id` is answered by walking `sqlite_autoindex_fact_reviews_1`, so the index
# chooses the order and which rowids this visits. Written as that walk alone -- `SELECT
# review_id,fact_id,review_json,active FROM fact_reviews ORDER BY review_id` -- the plan took
# `review_id` from the index KEY (`Column` on the index cursor) and only the other three
# columns from the row, so the digest pinned three of the four cells of a row and the key of
# the fourth: a table cell edited away from its key digested as the key, and the marker still
# matched byte for byte. Nothing reads that cell back today, which made it an accident of the
# call sites rather than a checked property. Joining the walk to the row it already seeks
# reads all four cells out of the TABLE b-tree, which is where `_current_row` reads the row it
# serves. `NOT INDEXED` on `t` says that side is the table and nothing else; a rowid is not an
# index, so the lookup it needs stays. It is inert against today's planner -- a rowid equality
# has one access path, so the plan is identical spelled without it -- and what actually checks
# where the cells come from is the plan pinned in the floor tests. `LEFT JOIN` keeps one
# streamed row per index entry, so an entry pointing at a rowid the table does not hold streams
# NULLs and is counted below rather than ending the read with SQLite's "database disk image is
# malformed", which an inner join would have dropped silently. Measured indistinguishable from
# the plain walk on M1-shaped rows -- 3.1 vs 3.2 ms, 17.7 vs 17.4 and 87.8 vs 87.3 at 386,
# 2,000 and 10,000 rows, medians of 21 interleaved reps, a difference that changes sign between
# sizes -- because it is the same seek the walk already deferred, landing on the same page,
# with one more cell read from it. `... FROM fact_reviews NOT INDEXED ORDER BY review_id` reads
# all four cells from the table as well, but sorts: 114 ms at 10,000 rows, with every review
# body through a temp b-tree.
_REVIEW_SELECT = ("SELECT t.review_id,t.fact_id,t.review_json,t.active FROM fact_reviews AS i "
                  "LEFT JOIN fact_reviews AS t NOT INDEXED ON t.rowid=i.rowid ORDER BY i.review_id")
# What the digest enumerates is still not the table: one streamed row per index entry, so a row
# present in the table b-tree but absent from `sqlite_autoindex_fact_reviews_1` streams through
# neither the digest nor the marker -- while `_current_row`, which resolves a rowid and re-reads
# the TABLE, finds and serves it. `NOT INDEXED` forbids the planner every index on
# `fact_reviews`, so this count is the table b-tree's own answer, and comparing the two is what
# makes "every row is digested" a checked claim rather than a property of whichever index the
# planner happened to pick. It decodes no row: one extra b-tree walk beside the one the digest
# already pays.
_REVIEW_COUNT = "SELECT count(*) FROM fact_reviews NOT INDEXED"
_SCHEMA_READ = "SELECT type,name FROM sqlite_master"
_CURRENT_INDEX = "CREATE INDEX IF NOT EXISTS fact_reviews_current ON fact_reviews(fact_id,active)"
# The owner's deselections. Absence is availability: a fact with no row here and no explicit review
# is implicitly reviewed. Created on reopen like the current-review index, before the schema pin, so a
# store an older engine wrote passes without a migration; it holds no rows until the owner opts out,
# and the authority digest keeps its pre-existing value until then.
# WITHOUT ROWID: the primary key IS the table's one b-tree, so there is no separate autoindex a row could
# be hidden from or served out of, and the hidden-row cross-check `fact_reviews` needs does not arise here.
_OPT_OUT_TABLE = ("CREATE TABLE IF NOT EXISTS fact_opt_outs(fact_id TEXT PRIMARY KEY,opted_out_at INTEGER NOT NULL,note TEXT)"
                  " WITHOUT ROWID")
_OPT_OUT_SELECT = "SELECT fact_id,opted_out_at,note FROM fact_opt_outs NOT INDEXED ORDER BY fact_id"
# Nothing bounds a store's lifetime rows, so the only backstop against unbounded growth
# is an operator noticing. This is the tripwire that says when a bounded or incremental
# digest has to be reconsidered; it carries a duration and a row count, never a review.
# 0.25 s was about 26,000 rows on the machine the change was measured on, well past the row
# count at which the per-write cost is already documented as too high. 0.1 s is about 10,600
# rows there; both are scaled from the one measured point, 94 ms at 10,000 rows with the
# table-count cross-check included (87 ms without it), so the warning
# arrives while the numbers still matter -- and it is measured around the cross-check, so it
# stays a statement about what one digest costs rather than about one part of it.
# The duration is machine-dependent by design: what matters is the cost paid while
# holding the node-wide write gate, not the row count that produced it.
_DIGEST_WARN_SECONDS = 0.1
# The exact-copy count behind `independent_copy_lineage`. `content=?1` is the predicate; the
# two expressions in front of it are the key of `idx_<table>_content_key`
# (`permissions_read_path_indexes_v1`), spelled from the same tuple so the planner answers
# them from that index and reads only the rows whose length and first 64 characters match,
# then checks the full text out of each row. On a database that has not run the migration
# the same statement scans the table, as the bare `content=?` did, and answers the same.
# The parameter goes through the same SQLite functions rather than being cut in Python:
# `length` counts characters before the first NUL and `substr` counts characters, and a
# Python-side `len`/slice would disagree with the index on exactly such a row.
_COPY_KEY = " AND ".join(f"{expression}={expression.replace('content', '?1', 1)}" for expression in CONTENT_KEY)
_COPY_COUNT = f"SELECT count(*) FROM {{table}} WHERE {_COPY_KEY} AND content=?1"
# Opaque fact keys completed in Python per read (lineage_keys.complete_pending), under the gate.
COMPLETION_BATCH = 64
_READ_ACTIONS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_RECURSIVE})
_ROW_WRITE_ACTIONS = frozenset({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})
_log = logging.getLogger(__name__)


class _Counted:
    """Counts the rows a digest streams, for the table cross-check and the slow-digest warning."""
    __slots__ = ("rows", "count")

    def __init__(self, rows):
        self.rows, self.count = rows, 0

    def __iter__(self):
        for row in self.rows:
            self.count += 1
            yield row


class _ReviewAccess:
    """A SQLite authorizer noting any compiled statement that could change a review row.

    Every statement on the connection is compiled through this hook, trigger
    programs included, and `set_authorizer` expires statements cached before it.
    Under BEGIN IMMEDIATE no other SQLite connection can commit a row change, so
    a transaction that never set `touched` wrote no review row through SQLite.
    Skipping the exit digest there is a refusal to look, not an equality: a
    non-SQLite writer that rewrites the store file underneath an open transaction
    does change the rows on disk, and re-reading them at the end is exactly the
    step that would publish the rewritten state into the marker as the owner's
    own. The next verifying open refuses that file instead.
    An unrecognized action code counts as touched: an unknown statement kind
    costs one extra digest rather than silently skipping one.
    `wrote` is any row write on any table, `touched` only a review row, so a
    transaction can write the observed clock high-water without paying a digest.
    """
    __slots__ = ("touched", "wrote")

    def __init__(self):
        self.touched = self.wrote = False

    def __call__(self, action, table, _column, _database, _trigger):
        if action in _READ_ACTIONS:
            return sqlite3.SQLITE_OK
        if action in _ROW_WRITE_ACTIONS:
            self.wrote = True
            if (table or "").lower() not in ("fact_reviews", "fact_opt_outs"):
                return sqlite3.SQLITE_OK
        self.touched = True
        return sqlite3.SQLITE_OK


class EvidenceBinding(StrictModel):
    """Pinned by the node runtime to this resource; never a recipient parameter."""
    environment_id: Identifier
    node_id: Identifier
    resource_id: Identifier
    owner_id: Identifier


class EvidenceIdentity(StrictModel):
    binding: EvidenceBinding
    table: Literal["signal_objects", "conversation_messages", "ai_chat_messages"]
    record_id: Identifier
    source_id: Identifier | None
    dataset_kind: Literal["row_dataset", "node_resource"]
    dataset_id: Identifier | None

    @model_validator(mode="after")
    def exact_scope(self):
        if self.table == "conversation_messages":
            if self.source_id is None or self.dataset_kind != "row_dataset" or self.dataset_id is None:
                raise ValueError("conversation identity requires dataset and source")
        elif self.dataset_kind != "node_resource" or self.dataset_id is not None:
            raise ValueError("datasetless table uses explicit node/resource scope")
        if self.table == "ai_chat_messages" and self.source_id is None:
            raise ValueError("message source missing")
        if self.table == "signal_objects" and self.source_id is not None:
            raise ValueError("derived fact has recursive sources, not a fabricated source")
        return self


class EvidenceRevision(StrictModel):
    identity: EvidenceIdentity
    revision: Hash


class EvidenceSnapshot(StrictModel):
    binding: EvidenceBinding
    canonical_file_revision: Hash
    fact_id: Identifier
    candidate_revision: Hash
    lineage_revision: Hash
    protection_revision: Hash
    artifacts: list[EvidenceRevision]
    leaves: list[EvidenceRevision]


class ReviewedClassification(StrictModel):
    """Explicit human attestation of one exact fact or leaf, not model output."""
    evidence: EvidenceRevision
    domains: list[Identifier]
    sensitivity: Literal["none", "personal", "special", "unknown"]
    subject_entity_ids: list[Identifier]
    authorship: Literal["owner_authored", "other", "unknown"]
    speech: Literal["direct_self_statement", "third_party_quote", "mixed", "unknown"]
    independent_copies: Literal["none_known", "present", "unknown"]


class OwnerEvidenceReview(StrictModel):
    version: Literal["topos-owner-evidence-review/v1"]
    review_id: Identifier
    owner_id: Identifier
    reviewed_at: Number
    snapshot: EvidenceSnapshot
    classifications: list[ReviewedClassification]


class QualifiedEvidence(StrictModel):
    """Private processing input. Does not permit any policy or output form.

    `subject_contract` records which owner-identity rule this evidence was
    qualified under, so a policy can never be evaluated against evidence
    qualified under a different one. It is set by the resolver from the signed
    capability, never by a caller.
    """
    family: Literal["owner_stated_fact/v1"]
    snapshot: EvidenceSnapshot
    review_id: Identifier
    review_revision: Hash
    classifications: list[ReviewedClassification]
    subject_contract: Literal["legacy_single_self_v1", "owner_attested_v1"]
    execution_enabled: Literal[False]
    # `explicit`: an owner review current for this snapshot. `implicit`: the node's labels, because the
    # owner has neither reviewed nor deselected the fact. Set by the resolver, never by a caller.
    review_mode: Literal["explicit", "implicit"] = "explicit"


def implicit_labels(payload: dict, dimension=None) -> tuple[tuple[str, ...], str]:
    """The owner-review-vocabulary/v1 labels a fact carries under implicit review."""
    predicate = payload.get("predicate") if isinstance(payload, dict) else None
    for key in (predicate, dimension):
        if isinstance(key, str) and key in IMPLICIT_LABELS:
            return IMPLICIT_LABELS[key]
    return IMPLICIT_FALLBACK


class _FrozenReviews:
    """The owner's decisions read once under the gate, for a build that runs outside it.

    Answers the two questions `_qualified_bundle` asks a review store -- the current explicit
    review of a fact and the set of deselected facts -- from an in-memory copy, so a p2c index
    can be built on a plain read snapshot; the publisher then checks the store's authority
    digest still matches the one frozen here.
    """
    def __init__(self, reviews: dict, opt_outs: frozenset, authority_digest: str):
        self.reviews, self.opt_outs, self.authority_digest = reviews, opt_outs, authority_digest

    def _current_in(self, _db, fact_id):
        return self.reviews.get(fact_id)

    def _opt_outs_in(self, _db):
        return self.opt_outs


class Qualification(StrictModel):
    verdict: Literal["qualified", "withheld"]
    reason_code: str
    evidence: QualifiedEvidence | None


QUALIFIED_REASON = {"explicit": "owner_reviewed_current_evidence", "implicit": "implicit_review_current_evidence"}


def _owner(binding: EvidenceBinding) -> None:
    principal = current_principal()
    if (principal is None or principal.cls != OWNER_APP or principal.channel not in {"uds", "cp_relay"}
        or principal.acting_user != binding.owner_id):
        raise PolicyError("owner_authority_required")


def _key(identity: EvidenceIdentity) -> str:
    return canonical_bytes(identity.model_dump()).decode("ascii")


def _json(raw, expected):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def constant(_):
        raise ValueError()
    try:
        if not isinstance(raw, str) or len(raw.encode()) > 1_048_576:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(value, expected):
            raise ValueError()
        return value
    except (ValueError, TypeError, RecursionError):
        raise PolicyError("evidence_malformed") from None


def _row_revision(row: dict, *, table: str | None = None) -> str:
    """Pin the reviewed surface of one row: every valued column but the table's operational ones.

    Without a table every column is pinned. A NULL column is absent from the
    surface, so a migration that adds a column stales nothing until a value
    appears in it; clearing a reviewed value is a change. A fact's payload is
    compared as sorted JSON without the extractor confidence a refresh
    rewrites; all other legacy JSON text is pinned exactly. The policy JSON
    grammar intentionally rejects floats, so SQLite floats are encoded as
    tagged exact hex strings solely for revision hashing.
    """
    excluded = REVIEW_SURFACE_EXCLUSIONS.get(table, frozenset())
    values = {}
    for name, value in row.items():
        if name in excluded or (value is None and table is not None):
            continue
        if table == "signal_objects" and name == "payload_json" and isinstance(value, str):
            payload = {key: item for key, item in _json(value, dict).items() if key != "confidence"}
            try:
                # Deterministic for equal values; this is a revision key, not
                # the float-free signed policy grammar, so floats stay allowed.
                values[name] = ["json", json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)]
            except (TypeError, ValueError):
                raise PolicyError("evidence_malformed") from None
        elif value is None:
            values[name] = ["null"]
        elif type(value) is int:
            values[name] = ["integer", str(value)]
        elif type(value) is float and math.isfinite(value):
            values[name] = ["float", value.hex()]
        elif isinstance(value, str):
            values[name] = ["text", value]
        elif isinstance(value, bytes):
            values[name] = ["blob", value.hex()]
        else:
            raise PolicyError("evidence_malformed")
    return digest(values)



def _source_posture(conn, identity: EvidenceIdentity) -> tuple[str, str]:
    """Snapshot-local restrictive mirror of native posture precedence.

    Do not call effective_posture/make_posture_resolver: their legacy fallback
    can open the ambient process database or suppress a failed override read.
    Missing legacy configuration inherits mixed, but never proves authorship.
    Malformed/ambiguous explicit configuration cannot silently inherit.
    """
    from topos.sources.registry import BUNDLED_REGISTRY

    valid = {"personal", "mixed", "ambient"}
    source = identity.source_id
    bundled = BUNDLED_REGISTRY.get(source)
    bundled_posture = getattr(bundled, "posture", None) if bundled is not None else None
    if bundled_posture is not None and (type(bundled_posture) is not str or bundled_posture not in valid):
        raise PolicyError("source_posture_unknown")

    def schema(table, required):
        found = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchmany(2)
        if not found:
            return False
        if len(found) != 1 or found[0][0] != "table":
            raise PolicyError("source_posture_unknown")
        columns = {item[1] for item in conn.execute(f"PRAGMA table_info({table})")}
        if not required <= columns:
            raise PolicyError("source_posture_unknown")
        return True

    override_present = schema("user_ingestion_sources", {"dataset_id", "source_id", "posture"})
    overrides = []
    if override_present:
        sql, args = "SELECT dataset_id,posture FROM user_ingestion_sources WHERE source_id=?", [source]
        if identity.dataset_kind == "row_dataset":
            sql += " AND dataset_id=?"
            args.append(identity.dataset_id)
        selected = conn.execute(sql, args).fetchmany(MAX_NODES + 1)
        if len(selected) > MAX_NODES:
            raise PolicyError("source_posture_unknown")
        datasets = set()
        for dataset, posture in selected:
            if (not isinstance(dataset, str) or not dataset or dataset != dataset.strip()
                or dataset in datasets or (posture is not None and (type(posture) is not str or posture not in valid))):
                raise PolicyError("source_posture_unknown")
            datasets.add(dataset)
            overrides.append({"dataset_id": dataset, "posture": posture})
        overrides.sort(key=lambda item: item["dataset_id"])

    runtime_present = schema("source_runtime_installs", {"source_id", "is_active", "status", "source_definition_json"})
    runtime_posture, runtime_revision = None, None
    if runtime_present:
        installed = conn.execute("SELECT * FROM source_runtime_installs WHERE source_id=? AND is_active IS NOT 0",
                                 (source,)).fetchmany(2)
        if len(installed) > 1:
            raise PolicyError("source_posture_unknown")
        if installed:
            installation = dict(installed[0])
            if type(installation["is_active"]) is not int or installation["is_active"] != 1 or installation["status"] not in {"installed", "active", "ready"}:
                raise PolicyError("source_posture_unknown")
            # A scoped source definition cannot lend a permissive posture to a
            # different owner, resource or dataset. Legacy schemas without a
            # scope column remain node-local; explicit malformed scopes reject.
            if "scope_key" in installation:
                scope = _json(installation["scope_key"], dict)
                if not scope or not set(scope) <= {"user_id", "device_id", "topos_id", "app_id", "dataset_id"}:
                    raise PolicyError("source_posture_unknown")
                scope_binding = {"user_id": identity.binding.owner_id, "topos_id": identity.binding.resource_id,
                                 "app_id": identity.binding.resource_id, "dataset_id": identity.dataset_id}
                for field, actual in scope.items():
                    if type(actual) is not str or not actual or actual != actual.strip():
                        raise PolicyError("source_posture_unknown")
                    if actual != "*" and (field == "device_id" or actual != scope_binding[field]):
                        raise PolicyError("source_posture_unknown")
            definition = _json(installation["source_definition_json"], dict)
            runtime_posture = definition.get("posture")
            if (("source_id" in definition and definition["source_id"] != source)
                or (runtime_posture is not None and (type(runtime_posture) is not str or runtime_posture not in valid))):
                raise PolicyError("source_posture_unknown")
            runtime_revision = _row_revision(installation)

    # Native registry semantics retain a non-mixed bundled declaration when a
    # runtime definition only carries the mixed default. Explicit personal or
    # ambient runtime declarations remain effective, subject to owner override.
    default = runtime_posture or bundled_posture or "mixed"
    if default == "mixed" and bundled_posture not in (None, "mixed"):
        default = bundled_posture
    explicit = [item["posture"] for item in overrides if item["posture"] is not None]
    if identity.dataset_kind == "row_dataset":
        effective = explicit[0] if explicit else default
    else:
        # Datasetless AI cannot borrow a dataset to erase an ambient cap. Any
        # ambient override for its source vetoes; permissive overrides cannot
        # relax an ambient source default without a certified dataset binding.
        effective = "ambient" if "ambient" in explicit else default
    revision = digest({"version": "evidence-source-posture/v1", "source_id": source,
        "dataset_kind": identity.dataset_kind, "dataset_id": identity.dataset_id,
        "override_schema_present": override_present, "overrides": overrides,
        "runtime_schema_present": runtime_present, "runtime_revision": runtime_revision,
        "runtime_posture": runtime_posture, "bundled_posture": bundled_posture, "effective": effective})
    return effective, revision

def _deleted(row: dict) -> bool:
    return any(row.get(field) not in (None, 0, False, "") for field in
               ("valid_to", "deleted_at", "is_deleted", "deleted"))


def _checked_file(path: Path, *, code: str, may_create: bool = False):
    """Reject symlinks throughout the explicit path, including after startup."""
    try:
        if not path.is_absolute() or ".." in path.parts:
            raise PolicyError(code)
        for parent in path.parents:
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise PolicyError(code)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if may_create:
                return None
            raise
        if not stat.S_ISREG(info.st_mode):
            raise PolicyError(code)
        return info
    except OSError:
        raise PolicyError(code) from None


class EvidenceResolver:
    """Read a fresh SQLite snapshot of one explicitly bound canonical database.

    Datasetless AI messages use node/resource identity; they do not acquire a
    conversation dataset label. Conversation references must name dataset_id.
    Unsupported leaf families and incomplete references are withheld.
    """
    def __init__(self, canonical_database: Path, *, binding: EvidenceBinding):
        path = Path(canonical_database)
        info = _checked_file(path, code="evidence_database_binding")
        self.path = path
        # In-process incarnation pin only. A bind mount renumbers device and
        # inode values across a VM restart, so they are never persisted and
        # never hashed into a durable identity.
        self._file_identity = (info.st_dev, info.st_ino)
        self.binding = EvidenceBinding.parse(binding.model_dump())
        self._clock_id = self._durable_clock_id()
        # The node-wide protection revision of the read in progress; adapters
        # compare it to signed authority. Snapshots bind their own closure.
        self.current_floor = None
        # The external canonical floor, attached by the node runtime. A
        # resolver constructed on its own has none, exactly as a review store
        # built outside enrollment has no external rollback floor.
        self.canonical_floor = None

    def _durable_clock_id(self) -> str:
        """The installed protection clock identity is this database's durable identity.

        The clock is installed exactly once, preserved by the explicit clock
        upgrade, and already persisted by every review store. A missing clock
        fails closed here instead of at the first read.
        """
        with with_db_write():
            self._incarnation()
            try:
                conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            except sqlite3.Error:
                raise PolicyError("evidence_storage_unavailable") from None
            try:
                conn.execute("BEGIN")
                return clock_state(conn)[0]
            except sqlite3.Error:
                raise PolicyError("evidence_storage_unavailable") from None
            finally:
                conn.close()

    def _incarnation(self) -> None:
        info = _checked_file(self.path, code="evidence_database_binding")
        if (info.st_dev, info.st_ino) != self._file_identity:
            raise PolicyError("evidence_database_binding")

    def _file_revision(self) -> str:
        """Durable canonical identity digest: binding, clock identity and exact path.

        Persisted enrollment markers, review stores, the ingest ledger and every
        review snapshot embed this value, so it must survive a restart or a
        remount of the same database. A different database has a different
        clock identity. A byte copy carries the same clock identity, so copies
        at the same path under the same binding are not told apart (container
        paths are fixed). An older copy restored in place is caught only once a
        review store or the ledger has observed the newer clock generation.
        """
        self._incarnation()
        return digest({"binding": self.binding.model_dump(), "clock_id": self._clock_id, "canonical_path": str(self.path)})

    @staticmethod
    def _keys_pending(conn) -> bool:
        """Whether an opaque fact still waits for Python keying; read inside the snapshot."""
        try:
            return lineage_keys.installed(conn) and conn.execute(
                "SELECT 1 FROM permissions_v2_fact_key_opaque WHERE state=0 LIMIT 1").fetchone() is not None
        except sqlite3.Error:
            return False

    def _complete_lineage_keys(self) -> None:
        """Key up to COMPLETION_BATCH opaque facts exactly, after a read that saw some; never raises.

        Opaque rows are always candidates until keyed, so their count would otherwise
        grow every read's cost with hidden facts in other scripts. Node start completes
        all of them (migration 78 is `always_run`); this keeps the backlog a node builds
        between starts draining by one batch per read, and costs nothing (no extra
        connection, whose schema parse alone is ~4 ms) when nothing waits. A failure
        leaves rows opaque, which costs time and never a candidate.
        """
        try:
            conn = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        except sqlite3.Error:
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if lineage_keys.installed(conn):
                    lineage_keys.complete_pending(conn, limit=COMPLETION_BATCH)
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 -- derived keys only; a failure costs time, never a candidate
            _log.debug("lineage key completion skipped")
        finally:
            conn.close()

    @contextmanager
    def _read(self, *, gated: bool = True):
        """One consistent read of the canonical database.

        Gated by default: consent writes and releases read and then decide under the node write
        gate. A p2c index build passes `gated=False` to read a snapshot without holding the gate
        (search_index.py, MERGE GATE); SQLite's own read transaction keeps that snapshot
        consistent, and the build's publisher re-checks floor, clock and reviews under the gate.
        """
        from contextlib import nullcontext
        with with_db_write() if gated else nullcontext():
            self._incarnation()
            pending = False
            try:
                conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            except sqlite3.Error:
                raise PolicyError("evidence_storage_unavailable") from None
            conn.row_factory = sqlite3.Row
            try:
                self._incarnation()
                conn.execute("BEGIN")
                # Requires the real canonical owner and intact monotonic clock.
                floor = current_protection_revision(conn, owner_id=self.binding.owner_id)
                if clock_state(conn)[0] != self._clock_id:
                    raise PolicyError("evidence_database_binding")
                # Every read, not only a consent write: the clock is monotone
                # inside its own file and cannot see that file being replaced.
                if self.canonical_floor is not None:
                    self.canonical_floor.check(conn)
                self.current_floor = floor
                pending = self._keys_pending(conn)
                yield conn, floor
                self._incarnation()
            except sqlite3.Error:
                raise PolicyError("evidence_storage_unavailable") from None
            finally:
                self.current_floor = None
                conn.close()
                if pending:
                    self._complete_lineage_keys()

    def _identity(self, table: str, record_id: str, source_id=None, dataset_id=None):
        return EvidenceIdentity.parse(dict(binding=self.binding.model_dump(), table=table, record_id=record_id,
            source_id=source_id, dataset_kind="row_dataset" if table == "conversation_messages" else "node_resource", dataset_id=dataset_id))

    def _reference(self, raw) -> EvidenceIdentity:
        if not isinstance(raw, dict) or not set(raw) <= {"table", "record_id", "source_id", "dataset_id", "node_id", "resource_id"}:
            raise PolicyError("lineage_identity_incomplete")
        if any(field in raw and raw[field] != getattr(self.binding, field) for field in ("node_id", "resource_id")):
            raise PolicyError("lineage_binding")
        try:
            return self._identity(raw.get("table"), raw.get("record_id"), raw.get("source_id"), raw.get("dataset_id"))
        except PolicyError:
            raise PolicyError("lineage_identity_incomplete") from None

    @staticmethod
    def _load(conn, identity: EvidenceIdentity) -> dict:
        # Table names are a closed enum and columns are selected only here.
        table = identity.table
        if table == "signal_objects":
            rows = conn.execute("SELECT * FROM signal_objects WHERE object_id=?", (identity.record_id,)).fetchmany(2)
        else:
            sql = f"SELECT * FROM {table} WHERE message_id=? AND source_id=?"
            args = [identity.record_id, identity.source_id]
            if table == "conversation_messages":
                sql += " AND dataset_id=?"
                args.append(identity.dataset_id)
            rows = conn.execute(sql, args).fetchmany(2)
        if not rows:
            raise PolicyError("evidence_missing")
        if len(rows) != 1:
            raise PolicyError("evidence_ambiguous")
        row = dict(rows[0])
        if any(marker in row for marker in ("_p2b_parent_revision", "_p2b_source_revision")):
            raise PolicyError("evidence_malformed")
        if _deleted(row):
            raise PolicyError("evidence_deleted")
        if table == "signal_objects" and row.get("object_type") != "fact":
            raise PolicyError("unsupported_derived_evidence")
        if table == "conversation_messages" and row.get("owner_user_id") != identity.binding.owner_id:
            raise PolicyError("evidence_owner_binding")
        if table == "ai_chat_messages":
            parents = conn.execute("SELECT * FROM ai_chat_conversations WHERE conversation_id=? AND source_id=?",
                (row.get("conversation_id"), identity.source_id)).fetchmany(2)
            if len(parents) != 1 or dict(parents[0]).get("owner_user_id") != identity.binding.owner_id:
                raise PolicyError("evidence_owner_binding")
            if _deleted(dict(parents[0])) or "_p2b_parent_revision" in row:
                raise PolicyError("evidence_malformed")
            row["_p2b_parent_revision"] = _row_revision(dict(parents[0]), table="ai_chat_conversations")
        if table in LEAF_TABLES:
            row["_p2b_source_revision"] = _source_posture(conn, identity)[1]
        return row

    def _validate_native_origin(self, conn, identity: EvidenceIdentity, row: dict) -> bool:
        """New owner-attested rows retain their revocable origin requirement.

        The reserved marker is inserted by the trusted canonical writer. It
        cannot authorize a legacy row: the separate durable service must prove
        the exact current enrollment, completed job and stored row identity
        (for an AI-chat row: its content revision and its conversation's owner).
        Existing untagged rows keep their independently required native/review
        checks only when they have no durable origin link. Losing the proof
        schema of an enrolled node cannot establish that absence. This shares
        the service's trusted-storage boundary, not an arbitrary host-tamper
        guarantee if both canonical history and private enrollment are erased.

        Returns True only when a live link was proven, False for an untagged
        row with no link. A tagged or linked row that fails raises.
        """
        if identity.table not in LEAF_TABLES:
            return False
        metadata = _json(row["metadata_json"], dict) if row.get("metadata_json") not in (None, "") else {}
        try:
            marker = self.path.parent / "permissions-v2" / "ingest-snapshots.enrollment.json"
            installed = conn.execute("SELECT 1 FROM sqlite_master WHERE name GLOB 'ingest_provenance_*' LIMIT 1").fetchone()
            if "topos_owner_ingest" not in metadata and not installed and not marker.exists() and not marker.is_symlink():
                return False
            from .ingest_provenance import IngestProvenanceService

            service = IngestProvenanceService(
                canonical_database=self.path, binding=self.binding,
                snapshot_root=self.path.parent / "permissions-v2" / "ingest-snapshots",
            )
            # The complete store must be intact before treating absent linkage
            # as a genuinely historical record rather than lost provenance.
            service._check(conn)
            if "topos_owner_ingest" not in metadata:
                if conn.execute("SELECT 1 FROM ingest_provenance_records WHERE message_id=?", (identity.record_id,)).fetchone():
                    raise PolicyError("native_owner_provenance_unavailable")
                return False
            service.validate_record_origin(conn, message_id=identity.record_id, origin=metadata["topos_owner_ingest"],
                                           table=identity.table)
            return True
        except Exception:
            raise PolicyError("native_owner_provenance_unavailable") from None

    def _ai_chat_owner_proven(self, conn, identity: EvidenceIdentity, row: dict) -> bool:
        """An AI-chat prompt is the owner's own words only through the attested ChatGPT lane.

        ``sender_type`` alone proves nothing: app_ingest defaults a missing role
        to "human", and a conversation's owner is just a dataset id prefix, so
        any writer that can reach those doors can mint a "human" row in the
        owner's conversation. The lane's source id on the row and on its one
        parent, the binding owner on that parent and a live origin link whose
        content revision matches the row are all required.
        """
        from .ingest_protocol import CHATGPT_SOURCE_ID

        if (row.get("sender_type") not in ("human", "user") or identity.source_id != CHATGPT_SOURCE_ID
                or row.get("source_id") != CHATGPT_SOURCE_ID):
            return False
        parents = conn.execute("SELECT owner_user_id,source_id FROM ai_chat_conversations WHERE conversation_id=?",
                               (row.get("conversation_id"),)).fetchmany(2)
        if len(parents) != 1 or tuple(parents[0]) != (self.binding.owner_id, CHATGPT_SOURCE_ID):
            return False
        return self._validate_native_origin(conn, identity, row)

    def _snapshot(self, conn, floor: str, fact_id: str, *, enforce_floor: bool = False):
        root = self._identity("signal_objects", fact_id)
        from .exclusion_floor import exclusions, fact_excluded
        tombstones = exclusions(conn)
        artifacts, leaves, rows, edges = {}, {}, {}, {}
        visiting = set()
        if enforce_floor and conn.execute("SELECT 1 FROM entity_blackholes LIMIT 1").fetchone():
            raise PolicyError("entity_protection_lineage_unavailable")
        if enforce_floor and tombstones["entity"]:
            raise PolicyError("entity_exclusion_lineage_unavailable")

        def visit(identity, depth):
            key = _key(identity)
            if depth > MAX_DEPTH:
                raise PolicyError("lineage_limit")
            if key in visiting:
                raise PolicyError("lineage_cycle")
            if key in rows:
                return
            if len(rows) >= MAX_NODES:
                raise PolicyError("lineage_limit")
            visiting.add(key)
            if enforce_floor and conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=? LIMIT 1",
                (identity.table, identity.record_id)).fetchone():
                raise PolicyError("owner_only")
            if enforce_floor and identity.record_id in tombstones["record"]:
                raise PolicyError("intelligence_excluded")
            row = self._load(conn, identity)
            self._validate_native_origin(conn, identity, row)
            if enforce_floor and identity.table == "signal_objects" and fact_excluded(
                _json(row.get("payload_json"), dict), tombstones["fact"], restriction_subjects(conn)):
                raise PolicyError("intelligence_excluded")
            rows[key] = row
            version = EvidenceRevision(identity=identity, revision=_row_revision(row, table=identity.table))
            if identity.table == "signal_objects":
                if enforce_floor and _json(row.get("payload_json"), dict).get("disclosure") not in SHAREABLE_DISCLOSURES:
                    raise PolicyError("owner_only")
                artifacts[key] = version
                refs = _json(row.get("source_refs_json"), list)
                if not refs:
                    raise PolicyError("lineage_missing")
                references = [self._reference(ref) for ref in refs]
                if len({_key(ref) for ref in references}) != len(references):
                    raise PolicyError("lineage_ambiguous")
                edges[key] = sorted(_key(ref) for ref in references)
                for reference in references:
                    visit(reference, depth + 1)
            else:
                leaves[key] = version
            visiting.remove(key)

        visit(root, 0)
        # The review binds the protection history of this closure, not of the
        # whole node: unrelated Off-limits edits no longer stale every review,
        # while any protect or lift of a closure record does. Entity floors
        # remain node-wide until coverage exists. `floor` stays the node-wide
        # revision for signed authority and is not bound here.
        scoped = closure_protection_revision(conn, owner_id=self.binding.owner_id,
            records=self._closure_records(artifacts, leaves), fact_prefixes=self._fact_prefixes(conn, rows, artifacts),
            identity=closure_identity(conn, subjects=self._closure_subjects(rows, artifacts),
                fact_ids={version.identity.record_id for version in artifacts.values()}))
        snapshot = EvidenceSnapshot(binding=self.binding, canonical_file_revision=self._file_revision(), fact_id=fact_id,
            candidate_revision=artifacts[_key(root)].revision,
            lineage_revision=digest({"artifacts": [artifacts[k].model_dump() for k in sorted(artifacts)],
                "leaves": [leaves[k].model_dump() for k in sorted(leaves)], "edges": edges}),
            protection_revision=scoped, artifacts=[artifacts[k] for k in sorted(artifacts)], leaves=[leaves[k] for k in sorted(leaves)])
        return snapshot, rows

    @staticmethod
    def _closure_records(artifacts, leaves):
        return {(version.identity.table, version.identity.record_id) for version in list(artifacts.values()) + list(leaves.values())}

    def _fact_prefixes(self, conn, rows, artifacts):
        """Tombstone prefixes this closure could match, over every owner spelling.

        This is a restriction, so it uses the widest set the node knows and never
        falls back: the old fallback to `{"self"}` silently dropped every
        entity-keyed owner tombstone on exactly the multi-self nodes that needed
        it most.
        """
        from topos.features.facts.store import normalize_predicate

        owners = restriction_subjects(conn)
        prefixes = set()
        for key in artifacts:
            payload = _json(rows[key].get("payload_json"), dict)
            subject, predicate = payload.get("subject_entity_id"), payload.get("predicate")
            if type(subject) is not str or type(predicate) is not str:
                # Malformed facts are withheld at read time; they bind no prefix.
                continue
            for candidate in (owners if subject in owners else {subject}):
                prefixes.add((candidate + ":" + normalize_predicate(predicate)).lower())
        return prefixes

    def _closure_subjects(self, rows, artifacts):
        """Entity ids this closure's facts name as subject or object."""
        subjects = set()
        for key in artifacts:
            payload = _json(rows[key].get("payload_json"), dict)
            for field in ("subject_entity_id", "object_entity_id"):
                value = payload.get(field)
                if type(value) is str and value:
                    subjects.add(value)
        return subjects

    @staticmethod
    def _names_a_leaf(raw, leaves: dict) -> bool:
        """Whether one fact's stored references name any leaf by (table, record_id).

        Source and dataset identity are ignored: legacy producers omit them.
        Ids and tables are compared stripped, and `id` counts beside
        `record_id`, as the node's own provenance reader does. Only a table
        naming a different evidence table rules a matching id out; a generic or
        malformed label does not. References that cannot be read (also with
        their JSON escapes decoded), or that carry no usable `record_id`, count
        when their text contains a leaf id, so a malformed or older writer
        fails closed.
        """
        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        try:
            refs = _json(text, list)
        except PolicyError:
            spelled = re.sub(r"\\u00([0-9A-Fa-f]{2})", lambda match: chr(int(match.group(1), 16)), text).replace("\\/", "/")
            return any(record_id in text or record_id in spelled for record_id in leaves)
        for ref in refs:
            value = ref.get("record_id") if isinstance(ref, dict) else None
            if type(value) not in (str, int) or not str(value).strip():
                if any(record_id in json.dumps(ref) for record_id in leaves):
                    return True
                continue
            table = ref.get("table")
            table = table.strip() if type(table) is str else None
            for candidate in (value, ref.get("id")):
                tables = leaves.get(str(candidate).strip()) if type(candidate) in (str, int) else None
                if tables and (table is None or table in tables or table not in ("signal_objects", *LEAF_TABLES)):
                    return True
        return False

    def _source_sibling_floor(self, conn, snapshot: EvidenceSnapshot, opted_out=frozenset()) -> None:
        """Raw release only: a leaf may not also back a fact the owner kept to themselves.

        Releasing a message discloses every claim drawn from it, not only the
        locator's. "I work at X and I live in Y" backs two facts, and the owner
        keeping either one to themselves -- by deselecting it (`opted_out`), or
        because its disclosure is one this node cannot share -- withholds the
        message. Every fact row naming a leaf, current, closed or deleted, is
        checked. A scalar release never calls this.

        One scan: SQLite keeps only rows whose reference text contains a leaf id,
        or a JSON escape through which an identifier's characters could be
        spelled (``\\u00XX``, ``\\/``). Only those rows are parsed. An
        Identifier has no GLOB metacharacter, so ``*id*`` is an exact substring
        test (and faster than instr); anything else falls back to instr.
        """
        leaves = {}
        for version in snapshot.leaves:
            leaves.setdefault(version.identity.record_id, set()).add(version.identity.table)
        if not leaves:
            return
        if lineage_keys.installed(conn):
            # Candidates by index: every fact keyed on a leaf id, plus every fact the keys
            # cannot speak for. A superset of what the scan below keeps (lineage_keys).
            rows = conn.execute("SELECT payload_json,source_refs_json,object_id FROM signal_objects WHERE object_type='fact' "
                                f"AND object_id IN ({lineage_keys.SIBLING_CANDIDATES.format(marks=','.join('?' * len(leaves)), ranges=' OR '.join(['(key>=? AND key<?)'] * len(leaves)))})",
                                lineage_keys.sibling_arguments(leaves))
        else:
            clauses, args = [], []
            for record_id in sorted(leaves):
                literal = not any(char in record_id for char in "*?[]")
                clauses.append("source_refs_json GLOB ?" if literal else "instr(source_refs_json,?)>0")
                args.append("*" + record_id + "*" if literal else record_id)
            rows = conn.execute("SELECT payload_json,source_refs_json,object_id FROM signal_objects WHERE object_type='fact' AND ("
                                + " OR ".join(clauses) + r" OR source_refs_json GLOB '*\u00*' OR source_refs_json GLOB '*\/*')",
                                args)
        for payload, refs, object_id in rows:
            if not self._names_a_leaf(refs, leaves):
                continue
            if object_id in opted_out:
                raise PolicyError("owner_opted_out")
            try:
                disclosure = _json(payload, dict).get("disclosure")
            except PolicyError:
                disclosure = None
            if disclosure not in SHAREABLE_DISCLOSURES:
                raise PolicyError("owner_only")

    def inspect_for_review(self, fact_id: str) -> EvidenceSnapshot:
        _owner(self.binding)
        with self._read() as (conn, floor):
            return self._snapshot(conn, floor, fact_id)[0]

    @staticmethod
    def _known_copies(conn, identity: EvidenceIdentity, row: dict) -> bool:
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            raise PolicyError("evidence_content_unknown")
        count = 0
        for table in LEAF_TABLES:
            # The first family requires both canonical table schemas so that
            # exact independent copies cannot hide in an unchecked sibling table.
            found = conn.execute(_COPY_COUNT.format(table=table), (content,)).fetchone()[0]
            count += found
        return count > 1

    def _eligible(self, conn, snapshot: EvidenceSnapshot, rows: dict, review: OwnerEvidenceReview, *, contract: str):
        """Two sets, never one: permits come from `contract`, vetoes from every owner spelling."""
        permits = permit_subjects(conn, contract=contract)
        restrictions = restriction_subjects(conn)
        from .exclusion_floor import exclusions, fact_excluded
        tombstones = exclusions(conn)
        expected = {_key(ref.identity): ref for ref in snapshot.artifacts + snapshot.leaves}
        classifications = {_key(item.evidence.identity): item for item in review.classifications}
        if len(classifications) != len(review.classifications) or set(classifications) != set(expected):
            raise PolicyError("classification_incomplete")
        # Entity mention lineage is not certified by this first adapter. A
        # protected entity anywhere conservatively withholds this fact family.
        if conn.execute("SELECT 1 FROM entity_blackholes LIMIT 1").fetchone():
            raise PolicyError("entity_protection_lineage_unavailable")
        if tombstones["entity"]:
            raise PolicyError("entity_exclusion_lineage_unavailable")
        for key, reference in expected.items():
            item = classifications[key]
            if item.evidence != reference:
                raise PolicyError("review_stale")
            if (not item.domains or len(item.domains) != len(set(item.domains)) or item.sensitivity == "unknown"
                or not item.subject_entity_ids or not self._labelled_subjects(item, contract, permits)
                or len(item.subject_entity_ids) != len(set(item.subject_entity_ids))):
                raise PolicyError("classification_unknown_or_mixed")
            if item.authorship != "owner_authored" or item.speech != "direct_self_statement":
                raise PolicyError("not_owner_self_statement")
            if item.independent_copies != "none_known":
                raise PolicyError("independent_copy_lineage")
            identity = reference.identity
            if identity.record_id in tombstones["record"]:
                raise PolicyError("intelligence_excluded")
            row = rows[key]
            if row.get("actor_role") not in (None, "authored"):
                raise PolicyError("not_owner_authored")
            if conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=? LIMIT 1",
                            (identity.table, identity.record_id)).fetchone():
                raise PolicyError("owner_only")
            if identity.table == "signal_objects":
                payload = _json(row.get("payload_json"), dict)
                if fact_excluded(payload, tombstones["fact"], restrictions):
                    raise PolicyError("intelligence_excluded")
                if payload.get("disclosure") not in SHAREABLE_DISCLOSURES:
                    raise PolicyError("owner_only")
                subject = payload.get("subject_entity_id")
                if isinstance(subject, str) and subject not in permits and subject in restrictions:
                    # A known owner spelling the owner has not attested, or whose
                    # identity moved since they did. Named separately so the owner
                    # can see why, while the recipient still sees one refusal.
                    raise PolicyError("owner_subject_unattested")
                if (not isinstance(subject, str) or subject not in permits
                    or payload.get("asserted_by") != "owner"
                    or ("actor_role" in payload and payload["actor_role"] != "authored")
                    or payload.get("object_entity_id") not in (None, "", *permits)):
                    raise PolicyError("not_owner_self_statement")
                if contract == ATTESTED_CONTRACT and rekeyed_facts(conn, [identity.record_id]):
                    # This fact's subject was rewritten in place, which is what a
                    # merge does to the absorbed entity's facts. Another person's
                    # claim can arrive this way carrying the owner's own messages
                    # as evidence, so it is never releasable under this contract.
                    raise PolicyError("fact_subject_rewritten")
                # Neither representation may mask an inferred/unknown one.
                # Native FactStore deliberately has no altitude; only that
                # writer's absence can be completed by this exact owner review
                # and the independently checked native authored source rows.
                altitudes = [value for value in (row.get("altitude"), payload.get("altitude")) if value is not None]
                if (any(value != "stated" for value in altitudes)
                    or (not altitudes and row.get("extractor_version") != "fact_store_v1")
                    or ("altitude" in payload and payload["altitude"] is None)):
                    raise PolicyError("unsupported_fact_altitude")
                if not isinstance(payload.get("predicate"), str) or not isinstance(payload.get("object_value"), str):
                    raise PolicyError("evidence_malformed")
                # Distinct active fact objects with the same normalized claim
                # are independent copies, not interchangeable lineage proofs.
                def normalized_claim(value):
                    subject = value.get("subject_entity_id")
                    if any(not isinstance(value.get(field), str) for field in ("subject_entity_id", "predicate", "object_value")):
                        raise PolicyError("evidence_malformed")
                    subject = "@owner" if subject in restrictions else subject
                    return tuple(" ".join(str(item or "").lower().split()) for item in
                                 (subject, value.get("predicate"), value.get("object_value")))
                claim = normalized_claim(payload)
                for other in self._claim_candidates(conn, claim, identity.record_id):
                    other_payload = _json(other[1], dict)
                    other_claim = normalized_claim(other_payload)
                    if claim == other_claim:
                        raise PolicyError("independent_copy_lineage")
            else:
                # Metadata review cannot turn an addressed/assistant row into
                # the owner's authored source. Native canonical role is required.
                if row.get("metadata_json") not in (None, ""):
                    metadata = _json(row["metadata_json"], dict)
                    # Signal reader/export quote keys, and an iMessage tapback's
                    # target (associated_message_type 0 is an ordinary message).
                    if any(metadata.get(field) not in (None, False, 0, "", [], {}) for field in
                           ("is_forwarded", "forwarded_from", "quoted_message", "quoted_text", "quote", "quoted_message_id", "quoted_sender", "is_quoted",
                            "quoteText", "quoteBody", "quoteAuthor", "quoteAuthorAci", "quoteAuthorUuid", "quoteId", "quotedMessageId",
                            "storyReplyContext", "associated_message_guid", "associated_message_type")):
                        raise PolicyError("not_owner_self_statement")
                if identity.table == "conversation_messages":
                    if type(row.get("is_from_self")) is not int or row["is_from_self"] != 1:
                        raise PolicyError("not_owner_authored")
                elif not self._ai_chat_owner_proven(conn, identity, row):
                    raise PolicyError("not_owner_authored")
                posture, _revision = _source_posture(conn, identity)
                if record_role(row, table=identity.table, posture=posture) != "authored":
                    raise PolicyError("not_owner_authored")
                if self._known_copies(conn, identity, row):
                    raise PolicyError("independent_copy_lineage")

    @staticmethod
    def _claim_candidates(conn, claim: tuple, record_id: str):
        """Active facts that could carry `claim`: by index when the keys are installed, else all of them.

        The candidates are a superset of the equal claims (lineage_keys); the caller's
        comparison decides. Rowid order, as the scan read them, so a node holding both a
        malformed fact and a copy names the same reason it did before.
        """
        if not lineage_keys.installed(conn):
            return conn.execute("SELECT object_id,payload_json FROM signal_objects WHERE object_type='fact' "
                                "AND valid_to IS NULL AND object_id<>?", (record_id,))
        key = lineage_keys.claim_key(claim[1], claim[2])
        return conn.execute("SELECT object_id,payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL "
                            "AND object_id<>? AND object_id IN (SELECT object_id FROM permissions_v2_fact_claim_keys WHERE claim_key=? "
                            "UNION SELECT object_id FROM permissions_v2_fact_key_completion WHERE family='claim' AND key=? "
                            "UNION SELECT object_id FROM permissions_v2_fact_key_opaque WHERE family='claim' AND state<>1)",
                            (record_id, key, key))

    def qualify(self, fact_id: str, *, reviews: "EvidenceReviewStore",
                contract: str = LEGACY_CONTRACT) -> Qualification:
        """Resolve now and load an authoritative stored review, never caller flags.

        `contract` defaults to the frozen legacy rule so that a caller which
        forgets it can only ever get today's behaviour. Widening to the attested
        rule is opt-in and comes from a signed capability.
        """
        try:
            return self.with_qualified(fact_id, reviews=reviews, contract=contract, callback=lambda evidence, _rows:
                Qualification(verdict="qualified", reason_code=QUALIFIED_REASON[evidence.review_mode], evidence=evidence))
        except PolicyError as exc:
            return Qualification(verdict="withheld", reason_code=exc.code, evidence=None)

    @staticmethod
    def _labelled_subjects(item, contract, permits) -> bool:
        """What the owner said the record is about.

        The label vocabulary does not change with the binding: `self` means
        "about me", and the attested contract resolves which entities that
        covers. Entity ids are never a label, so no review carries one and none
        reaches the control plane, the frontend or a recipient.
        """
        labels = set(item.subject_entity_ids)
        if len(labels) != len(item.subject_entity_ids):
            return False
        if contract == ATTESTED_CONTRACT:
            return labels == {"self"}
        return labels <= permits

    def _implicit_review(self, conn, snapshot: EvidenceSnapshot, rows: dict, *, contract: str) -> OwnerEvidenceReview:
        """The review a fact carries when the owner has neither reviewed nor deselected it.

        Its labels are the node's own (`implicit_labels`), its authorship, speech and copy
        claims are exactly what `_eligible` then verifies against the rows, and its snapshot is
        the one just taken, so it can never be stale. It is never stored: revoking nothing
        leaves the fact available, and an opt-out row is what withholds it.
        """
        root = next(rows[_key(version.identity)] for version in snapshot.artifacts
                    if version.identity.record_id == snapshot.fact_id)
        payload = _json(root.get("payload_json"), dict)
        domains, sensitivity = implicit_labels(payload, root.get("signal_dimension"))
        subject = payload.get("subject_entity_id")
        subjects = ["self"] if contract == ATTESTED_CONTRACT else ([subject] if isinstance(subject, str) else [])
        classifications = [ReviewedClassification(evidence=version, domains=list(domains), sensitivity=sensitivity,
            subject_entity_ids=subjects, authorship="owner_authored", speech="direct_self_statement",
            independent_copies="none_known") for version in snapshot.artifacts + snapshot.leaves]
        return OwnerEvidenceReview(version="topos-owner-evidence-review/v1",
            review_id=IMPLICIT_REVIEW_PREFIX + snapshot.fact_id, owner_id=self.binding.owner_id, reviewed_at=0,
            snapshot=snapshot, classifications=classifications)

    def _qualified_bundle(self, conn, floor, fact_id, reviews, review_db, *, contract=LEGACY_CONTRACT,
                          discloses_sources=False):
        snapshot, rows = self._snapshot(conn, floor, fact_id, enforce_floor=True)
        opted_out = reviews._opt_outs_in(review_db)
        # The owner's deselection is the one signal that beats everything else, an explicit
        # review included: it is what "the user can deselect items" means.
        if fact_id in opted_out:
            raise PolicyError("owner_opted_out")
        if discloses_sources:
            self._source_sibling_floor(conn, snapshot, opted_out=opted_out)
        review, mode = reviews._current_in(review_db, fact_id), "explicit"
        if review is None:
            review, mode = self._implicit_review(conn, snapshot, rows, contract=contract), "implicit"
        if review.owner_id != self.binding.owner_id or review.snapshot != snapshot:
            raise PolicyError("review_stale")
        self._eligible(conn, snapshot, rows, review, contract=contract)
        return QualifiedEvidence(family="owner_stated_fact/v1", snapshot=snapshot, review_id=review.review_id,
            review_revision=digest(review.model_dump()), classifications=review.classifications,
            subject_contract=contract, execution_enabled=False, review_mode=mode), rows

    def with_qualified(self, fact_id: str, *, reviews: "EvidenceReviewStore", callback,
                       contract: str = LEGACY_CONTRACT, discloses_sources: bool = False):
        """Run trusted server code with current private evidence under both gates.

        This callback is never deserialized from a request. The service assumes
        the configured single writer and shared node write gate; bypassing them
        with an external WAL writer is outside that transaction guarantee.
        The callback must not mutate canonical data or reviews. It must perform
        its own final policy/authority checks before releasing any output.
        A caller that will release leaf content sets `discloses_sources`, which
        adds the sibling-fact floor inside this same read.
        """
        if contract not in SUBJECT_CONTRACTS:
            raise PolicyError("subject_contract_unknown")
        if reviews.binding != self.binding or reviews.canonical_file_revision != self._file_revision():
            raise PolicyError("review_database_binding")
        with self._read() as (conn, floor):
            reviews._observe_clock(conn)
            with reviews._db() as review_db:
                evidence, rows = self._qualified_bundle(conn, floor, fact_id, reviews, review_db, contract=contract,
                                                        discloses_sources=discloses_sources)
                return callback(evidence, rows)


class EvidenceReviewStore:
    """Private node-local owner review metadata with an observed clock high-water.

    This explicit SQLite file is a trusted service dependency, never a request
    object. Its durable identity is a random `store_id` persisted inside the
    file and, once enrolled, in the private external marker; device/inode are
    only an in-process incarnation pin. An enrolled store also carries an
    external authority digest so an in-place restore of an older file is
    detected as rollback. It is not an integrity boundary against a privileged
    host administrator who rewrites every trusted file together.
    """
    # Exactly the objects this store creates, by kind and name. Anything else is refused:
    # a trigger or a view executes, and an index decides which row a predicate answers with,
    # while the authority digest covers rows and never schema. Deny by default, because a
    # kind this list does not know about must fail closed rather than be allowed by omission.
    # `type` is compared lower-cased: SQLite decides an object's kind from its `sql` text and
    # accepts any case variant in `type`, so `type='TRIGGER'` installs a trigger that fires
    # (verified on SQLite 3.47.1), and a `type IN ('trigger','view')` test misses it. `sql`
    # is deliberately not compared: rewriting it cannot smuggle in an executing object, and
    # any reinterpretation of the stored cells moves the row digest.
    _schema_objects = frozenset({("table", "review_identity"), ("table", "fact_reviews"), ("table", "fact_opt_outs"),
        ("index", "sqlite_autoindex_fact_reviews_1"), ("index", "fact_reviews_current")})

    def __init__(self, path: Path, *, resolver: EvidenceResolver, _existing_only=False):
        if not _existing_only:
            _owner(resolver.binding)
        path = Path(path)
        _checked_file(path, code="review_database_binding", may_create=not _existing_only)
        if path == resolver.path:
            raise PolicyError("review_database_binding")
        self.path, self.binding = path, resolver.binding
        self._resolver = resolver
        self.canonical_file_revision = resolver._file_revision()
        try:
            try:
                if _existing_only:
                    raise FileExistsError()
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                newly_created = True
            except FileExistsError:
                fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
                newly_created = False
            try:
                info = os.fstat(fd)
                self._file_identity = (info.st_dev, info.st_ino)
            finally:
                os.close(fd)
        except OSError:
            raise PolicyError("review_database_binding") from None
        if self._file_identity == resolver._file_identity:
            raise PolicyError("review_database_binding")
        self._binding_json = canonical_bytes(self.binding.model_dump()).decode("ascii")
        self.store_id = None
        # Attached by the enrollment runtime after it verified the marker; a
        # standalone store has no external rollback floor.
        self._floor = None
        with resolver._read() as (conn, _floor):
            self._clock_id, self._highest_generation = clock_state(conn)
            with self._db(initializing=True) as db:
                if newly_created:
                    self.store_id = secrets.token_hex(32)
                    db.execute("CREATE TABLE review_identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1),binding_json TEXT NOT NULL,file_revision TEXT NOT NULL,clock_id TEXT NOT NULL,highest_generation INTEGER NOT NULL,store_id TEXT NOT NULL)")
                    db.execute("CREATE TABLE fact_reviews(review_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL,review_json TEXT NOT NULL,active INTEGER NOT NULL CHECK(active IN (0,1)))")
                    db.execute(_CURRENT_INDEX)
                    db.execute(_OPT_OUT_TABLE)
                    db.execute("INSERT INTO review_identity VALUES(1,?,?,?,?,?)", (self._binding_json,
                        self.canonical_file_revision, self._clock_id, self._highest_generation, self.store_id))
                else:
                    # Loss of a durable identity/clock is not first enrollment.
                    # Do not rebuild either schema or singleton in existing files.
                    old = db.execute(_IDENTITY_SELECT).fetchone()
                    self._check_identity(old, reopening=True)
                    # The current-review lookup is otherwise a table scan, which grows with
                    # lifetime reviews now that nothing caps them. An index is not part of
                    # the digested state, so adding one to an existing store changes no
                    # pinned value and leaves the marker untouched. It is created before the
                    # schema pin below, which requires it: that is what lets a store an older
                    # engine wrote pass the pin without a migration step of its own. DDL fires
                    # no trigger, and a refusal below rolls this whole transaction back.
                    db.execute(_CURRENT_INDEX)
                    db.execute(_OPT_OUT_TABLE)
                    # Before the reopen's own high-water write, so a trigger planted on
                    # `review_identity` can never fire: this transaction predates the floor,
                    # and so predates the verifying open that would otherwise catch it.
                    self._check_schema(db)
                    db.execute("SELECT review_id,fact_id,review_json,active FROM fact_reviews LIMIT 1")
                    db.execute("UPDATE review_identity SET highest_generation=? WHERE singleton=1", (self._highest_generation,))

    def _check_schema(self, db):
        """Refuse any object in the store file that this store did not create."""
        found = {(kind.lower() if type(kind) is str else kind, name)
                 for kind, name in db.execute(_SCHEMA_READ)}
        if found != self._schema_objects:
            raise PolicyError("review_database_binding")

    def _check_file(self):
        info = _checked_file(self.path, code="review_database_binding")
        if (info.st_dev, info.st_ino) != self._file_identity:
            raise PolicyError("review_database_binding")
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise PolicyError("review_store_permissions")
        if self._resolver._file_revision() != self.canonical_file_revision:
            raise PolicyError("review_database_binding")

    def _check_identity(self, row, *, reopening=False):
        if row is None or tuple(row[:3]) != (self._binding_json, self.canonical_file_revision, self._clock_id):
            raise PolicyError("review_database_binding")
        store_id = row[4]
        if (type(store_id) is not str or _STORE_ID.fullmatch(store_id) is None
            or (self.store_id is not None and store_id != self.store_id)):
            raise PolicyError("review_database_binding")
        generation = row[3]
        if type(generation) is not int or not 0 <= generation <= MAX_INTEGER:
            raise PolicyError("review_protection_clock")
        # A new process must not reopen against an older canonical clock. A
        # running process must not observe its private high-water moving back.
        if (reopening and generation > self._highest_generation) or (not reopening and generation < self._highest_generation):
            raise PolicyError("review_protection_clock")
        if reopening and self.store_id is None:
            self.store_id = store_id
        if not reopening:
            self._highest_generation = generation

    @staticmethod
    def _authority_digest(db) -> str:
        """Digest of every review row; the observed clock high-water is excluded.

        The value is exactly `digest([[review_id, fact_id, review_json, active], ...])`
        over every row ordered by `review_id`, retired rows included. It is streamed
        into SHA-256 rather than built as one canonical value, so the store is no
        longer capped by the 1 MiB canonical encoding limit, which stopped one store
        at 309 owner reviews of this shape. The bytes, and so every pinned digest,
        are unchanged.

        Every cell it hashes is read out of the table b-tree, and what the index
        decides instead is checked rather than trusted. `_REVIEW_SELECT` walks
        `sqlite_autoindex_fact_reviews_1` and joins each entry to the row it points
        at, so that index decides two things: the ORDER of the stream, and which
        rowids it reaches. The order is pinned by the digest itself, since the same
        rows in another order are another value. The reach is not: hardening
        `_current_row` against `fact_reviews_current` left the digest resting on a
        different index, and a row hidden from the primary-key autoindex is
        invisible here, so the marker still matches, while `_current_row`'s rowid
        re-read serves that row as the owner's current review. The streamed row
        count is therefore compared with the table's own count, taken with `NOT
        INDEXED`. Both reads run in one transaction under `BEGIN IMMEDIATE`, so
        nothing can commit between them and a disagreement is never a race: it is
        refused as `review_database_binding`, the same quarantining code the schema
        pin and the `total_changes` cross-check use, rather than as the transient
        `review_storage_unavailable`. The comparison is an equality in both
        directions. A stream shorter than the table is the hidden row. A stream
        longer than it is an index entry the table cannot answer for, which the
        entry digest would refuse anyway as a changed value, but which the exit
        digest -- where a changed value is published rather than refused -- would
        otherwise write into the marker as the owner's own work. At the entry digest
        the count and the value are exhaustive together: a stream that misses a
        table row and still counts right streamed something in its place -- some
        other row a second time, or an entry with no row behind it, which comes
        through as NULLs -- and both of those are in the value. This runs at the
        entry digest and at the exit digest alike, because both are this function.

        It does not check `fact_reviews_current` the same way, and does not need
        to: a row hidden from that index is still in the autoindex, so it is still
        digested and the marker still has to match it, and what such an index can
        do -- decide which row answers `fact_id=? AND active=1` -- `_current_row`
        already refuses to trust. `PRAGMA integrity_check` would catch both, and
        every other b-tree fault besides. It is deliberately not on this path, and
        the reason is NOT that it is slow: measured on this store's shape it is
        about a quarter of one digest (0.6 ms, 4.3 ms and 25 ms at 386, 2,000 and
        10,000 rows, against 3.4 ms, 17 ms and 87 ms), because the digest's cost is
        encoding rows in Python while its is walking pages in C. The reasons are
        that its cost is bounded by the whole FILE rather than by this one table,
        so it grows with anything else the store ever holds; that its verdict is a
        list of English sentences rather than a value, so reading "anything but ok"
        as a refusal makes a SQLite message-text change either a node outage or a
        silent pass; and that it is still 3-4x the cross-check's own cost (+18% and
        +29% of a digest at 386 and 10,000 rows, against +0.5% and +7.6%) inside
        the section that holds the node-wide write gate. It stays the operator-side
        check, which is where the row this cross-check refuses is diagnosed.
        """
        # The owner's deselections are authority too: a store rolled back to before an opt-out would
        # widen release, so once any exist they enter the digest. With none, the value is exactly the
        # one every earlier engine computed, so an existing marker keeps matching without migration.
        # A file with no such table (a store an older engine wrote, before its reopen creates it) has
        # no deselections; the reopen creates the table before the schema pin.
        try:
            opt_outs = [list(row) for row in db.execute(_OPT_OUT_SELECT)]
        except sqlite3.OperationalError:
            opt_outs = []
        counted = _Counted(db.execute(_REVIEW_SELECT))
        started = time.monotonic()
        if opt_outs:
            result = digest_stream({"opt_outs": opt_outs, "reviews": Rows(counted)})
        else:
            result = digest_stream(Rows(counted))
        stored = db.execute(_REVIEW_COUNT).fetchone()[0]
        elapsed = time.monotonic() - started
        if counted.count != stored:
            raise PolicyError("review_database_binding")
        if elapsed > _DIGEST_WARN_SECONDS:
            _log.warning("permissions_v2 review authority digest took %d ms over %d rows",
                         int(elapsed * 1000), counted.count)
        return result

    def current_authority_digest(self) -> str:
        with self._db() as db:
            return self._authority_digest(db)

    @contextmanager
    def _db(self, *, initializing=False):
        with with_db_write():
            self._check_file()
            try:
                db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            except sqlite3.Error:
                raise PolicyError("review_storage_unavailable") from None
            try:
                self._check_file()
                db.execute("BEGIN IMMEDIATE")
                floor = None if initializing else self._floor
                access, changes = None, db.total_changes
                if not initializing:
                    self._check_identity(db.execute(_IDENTITY_SELECT).fetchone())
                    if floor is not None:
                        self._check_schema(db)
                        if self._authority_digest(db) != floor.expected_authority_digest():
                            raise PolicyError("review_store_rollback")
                        access = _ReviewAccess()
                        db.set_authorizer(access)
                yield db
                self._check_file()
                published = None
                if floor is not None:
                    if access.touched:
                        after = self._authority_digest(db)
                        if after != floor.expected_authority_digest():
                            # Durable before the commit. A crash in either order
                            # leaves the enrollment pending, never a silent reset.
                            floor.publish_pending(after)
                            published = after
                    elif not access.wrote and db.total_changes != changes:
                        # Nothing compiled here could change any row, so a changed row is a
                        # write this connection's authorizer never saw. That is a tamper
                        # signal, not a transient fault, so it gets the floor's own refusal
                        # rather than `review_storage_unavailable`. It can only be read as a
                        # statement about transactions that compiled no row write at all:
                        # `total_changes` is connection-wide, so a legitimate clock write
                        # would otherwise trip it, which is why `wrote` gates the branch.
                        raise PolicyError("review_database_binding")
                db.commit()
                if published is not None:
                    floor.publish_active(published)
            except sqlite3.Error:
                db.rollback()
                raise PolicyError("review_storage_unavailable") from None
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def _observe_clock(self, canonical_conn):
        clock_id, generation = clock_state(canonical_conn)
        with self._db() as db:
            if clock_id != self._clock_id or generation < self._highest_generation:
                raise PolicyError("review_protection_clock")
            # Only when it actually moves. An unconditional UPDATE made every owner read a
            # writing transaction, which both amplified writes and switched off the
            # `total_changes` cross-check below for the one path that has nothing else to
            # write. The stored value was just read and validated by `_check_identity`.
            if db.execute("SELECT highest_generation FROM review_identity WHERE singleton=1").fetchone()[0] != generation:
                db.execute("UPDATE review_identity SET highest_generation=? WHERE singleton=1", (generation,))
        self._highest_generation = generation

    def record_review(self, *, resolver: EvidenceResolver, review_id: str, expected_snapshot: EvidenceSnapshot,
                      classifications: list[ReviewedClassification], reviewed_at: int,
                      expected_current_review_revision=_ANY_REVIEW, _server_timestamp_retry=False) -> OwnerEvidenceReview:
        _owner(self.binding)
        if resolver.binding != self.binding or resolver._file_revision() != self.canonical_file_revision:
            raise PolicyError("review_database_binding")
        # The owner must have inspected these exact revisions. A concurrent
        # canonical mutation after this snapshot makes the review stale on use.
        with resolver._read() as (conn, floor):
            self._observe_clock(conn)
            current = resolver._snapshot(conn, floor, expected_snapshot.fact_id)[0]
            if current != expected_snapshot:
                raise PolicyError("review_stale")
            review = OwnerEvidenceReview.parse(dict(version="topos-owner-evidence-review/v1", review_id=review_id,
                owner_id=self.binding.owner_id, reviewed_at=reviewed_at, snapshot=current.model_dump(),
                classifications=[item.model_dump() for item in classifications]))
            with self._db() as db:
                old = db.execute("SELECT review_json,active FROM fact_reviews WHERE review_id=?", (review.review_id,)).fetchone()
                raw = canonical_bytes(review.model_dump()).decode("ascii")
                if old:
                    stored = OwnerEvidenceReview.parse(old[0])
                    same = (stored.model_dump(exclude={"reviewed_at"}) == review.model_dump(exclude={"reviewed_at"})
                            if _server_timestamp_retry else old[0] == raw)
                    if not same or old[1] != 1:
                        raise PolicyError("review_id_conflict")
                    return stored
                if expected_current_review_revision is not _ANY_REVIEW:
                    current_review = self._current_in(db, current.fact_id)
                    actual = digest(current_review.model_dump()) if current_review else None
                    if actual != expected_current_review_revision:
                        raise PolicyError("review_conflict")
                db.execute("UPDATE fact_reviews SET active=0 WHERE fact_id=? AND active=1", (current.fact_id,))
                db.execute("INSERT INTO fact_reviews VALUES(?,?,?,1)", (review.review_id, current.fact_id, raw))
            return review

    def revoke_review(self, review_id: str, *, fact_id=None, expected_review_revision=_ANY_REVIEW) -> OwnerEvidenceReview | None:
        _owner(self.binding)
        with self._db() as db:
            if expected_review_revision is not _ANY_REVIEW:
                row = db.execute("SELECT review_json,active FROM fact_reviews WHERE review_id=? AND fact_id=?", (review_id, fact_id)).fetchone()
                if row is None:
                    raise PolicyError("review_unknown")
                review = OwnerEvidenceReview.parse(row[0])
                if digest(review.model_dump()) != expected_review_revision:
                    raise PolicyError("review_conflict")
                current_review = self._current_in(db, fact_id)
                if current_review is not None and current_review.review_id != review_id:
                    raise PolicyError("review_conflict")
            # Deliberately not narrowed with `AND active=1`, unlike `record_review`'s retire:
            # `rowcount == 1` below is this call's idempotency check, so matching only active
            # rows would turn a repeated revoke into `review_unknown`. Pinned by
            # tests/permissions_v2/test_evidence_reviews.py::
            # test_review_server_time_retry_current_revision_cas_and_revoke, which fails on
            # the narrowed spelling.
            changed = db.execute("UPDATE fact_reviews SET active=0 WHERE review_id=?", (review_id,))
            if changed.rowcount != 1:
                raise PolicyError("review_unknown")
            return review if expected_review_revision is not _ANY_REVIEW else None

    # --- the owner's deselections --------------------------------------------------------------

    def opt_out(self, fact_id: str, *, now: int, note: str | None = None) -> bool:
        """Deselect one fact: withheld from every policy until opted in again. Idempotent."""
        _owner(self.binding)
        with self._db() as db:
            if db.execute("SELECT 1 FROM fact_opt_outs WHERE fact_id=?", (fact_id,)).fetchone():
                return False
            db.execute("INSERT INTO fact_opt_outs VALUES(?,?,?)", (fact_id, now, note))
            return True

    def opt_in(self, fact_id: str) -> bool:
        """Reselect one fact: implicitly reviewed again from now on. Idempotent."""
        _owner(self.binding)
        with self._db() as db:
            return db.execute("DELETE FROM fact_opt_outs WHERE fact_id=?", (fact_id,)).rowcount == 1

    @staticmethod
    def _opt_outs_in(db) -> frozenset:
        return frozenset(row[0] for row in db.execute("SELECT fact_id FROM fact_opt_outs"))

    @staticmethod
    def _opted_out_in(db, fact_id) -> bool:
        return db.execute("SELECT 1 FROM fact_opt_outs WHERE fact_id=?", (fact_id,)).fetchone() is not None

    def freeze(self, db) -> "_FrozenReviews":
        """Every current explicit review and every opt-out, plus the digest that pins them, for a build outside the gate."""
        reviews = {}
        for fact_id, in db.execute("SELECT DISTINCT fact_id FROM fact_reviews WHERE active=1"):
            review = self._current_in(db, fact_id)
            if review is not None:
                reviews[fact_id] = review
        return _FrozenReviews(reviews, self._opt_outs_in(db), self._authority_digest(db))

    @staticmethod
    def _current_row(db, fact_id, code="review_ambiguous"):
        """The one active row for `fact_id`, read out of the table rather than out of the index.

        `WHERE fact_id=? AND active=1` is answered from `fact_reviews_current`'s keys, so a
        stale or planted b-tree under that name would otherwise decide which review is
        current while the row digest still matched the marker byte for byte. The rowid
        lookup goes to the table b-tree, and the flags are re-asserted from what it
        returns, so serving a revoked review needs a real row in the table b-tree -- which
        the entry digest refuses, including the row the digest's own enumeration cannot
        see, because `_authority_digest` compares what it streamed with the table's own
        `NOT INDEXED` count. Reading the row out of the table is only worth anything while
        the table is what the digest is taken over. `PRAGMA integrity_check` is the
        operator-side check for the index itself; nothing in a request path runs it.
        """
        found = db.execute("SELECT rowid FROM fact_reviews WHERE fact_id=? AND active=1", (fact_id,)).fetchmany(2)
        if len(found) > 1:
            raise PolicyError(code)
        if not found:
            return None
        row = db.execute("SELECT fact_id,review_json,active FROM fact_reviews WHERE rowid=?", (found[0][0],)).fetchone()
        if row is None or row[0] != fact_id or row[2] != 1:
            raise PolicyError("review_database_binding")
        return row[1]

    @staticmethod
    def _current_in(db, fact_id):
        body = EvidenceReviewStore._current_row(db, fact_id)
        return OwnerEvidenceReview.parse(body) if body is not None else None

    def _load_current(self, fact_id: str) -> OwnerEvidenceReview:
        with self._db() as db:
            review = self._current_in(db, fact_id)
            if review is None:
                raise PolicyError("owner_review_required")
            return review
