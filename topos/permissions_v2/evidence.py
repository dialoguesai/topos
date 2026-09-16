"""Node-local fact evidence qualification; neither a grant nor a release API.

Only existing scoped facts with explicitly reviewed, current recursive evidence
can qualify. Reviews are owner-authored in a separate private store. Pack names,
legacy confirmation flags and recipient-supplied labels confer no authority.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Literal

from pydantic import model_validator

from topos.principal import OWNER_APP, current_principal
from topos.features.provenance.roles import record_role
from topos.storage.db.write_gate import with_db_write

from .canonical import MAX_INTEGER, PolicyError, canonical_bytes, digest
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
_IDENTITY_SELECT = "SELECT binding_json,file_revision,clock_id,highest_generation,store_id FROM review_identity WHERE singleton=1"
_STORE_ID = re.compile(r"[0-9a-f]{64}")


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


class Qualification(StrictModel):
    verdict: Literal["qualified", "withheld"]
    reason_code: str
    evidence: QualifiedEvidence | None


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

    @contextmanager
    def _read(self):
        with with_db_write():
            self._incarnation()
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
                yield conn, floor
                self._incarnation()
            except sqlite3.Error:
                raise PolicyError("evidence_storage_unavailable") from None
            finally:
                self.current_floor = None
                conn.close()

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
                if enforce_floor and _json(row.get("payload_json"), dict).get("disclosure") != "scoped":
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

    def _source_sibling_floor(self, conn, snapshot: EvidenceSnapshot) -> None:
        """Raw release only: a leaf may not also back a fact the owner kept to themselves.

        Releasing a message discloses every claim drawn from it, not only the
        locator's. "I work at X and I live in Y" backs a scoped fact and an
        owner-only one, and the owner-only claim is outside the reviewed
        closure. Every fact row naming a leaf, current, closed or deleted, must
        be exactly scoped. A scalar release never calls this.

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
        clauses, args = [], []
        for record_id in sorted(leaves):
            literal = not any(char in record_id for char in "*?[]")
            clauses.append("source_refs_json GLOB ?" if literal else "instr(source_refs_json,?)>0")
            args.append("*" + record_id + "*" if literal else record_id)
        rows = conn.execute("SELECT payload_json,source_refs_json FROM signal_objects WHERE object_type='fact' AND ("
                            + " OR ".join(clauses) + r" OR source_refs_json GLOB '*\u00*' OR source_refs_json GLOB '*\/*')",
                            args)
        for payload, refs in rows:
            if not self._names_a_leaf(refs, leaves):
                continue
            try:
                disclosure = _json(payload, dict).get("disclosure")
            except PolicyError:
                disclosure = None
            if disclosure != "scoped":
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
            found = conn.execute(f"SELECT count(*) FROM {table} WHERE content=?", (content,)).fetchone()[0]
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
                if payload.get("disclosure") != "scoped":
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
                for other in conn.execute("SELECT object_id,payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL AND object_id<>?", (identity.record_id,)):
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

    def qualify(self, fact_id: str, *, reviews: "EvidenceReviewStore",
                contract: str = LEGACY_CONTRACT) -> Qualification:
        """Resolve now and load an authoritative stored review, never caller flags.

        `contract` defaults to the frozen legacy rule so that a caller which
        forgets it can only ever get today's behaviour. Widening to the attested
        rule is opt-in and comes from a signed capability.
        """
        try:
            return self.with_qualified(fact_id, reviews=reviews, contract=contract, callback=lambda evidence, _rows:
                Qualification(verdict="qualified", reason_code="owner_reviewed_current_evidence", evidence=evidence))
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

    def _qualified_bundle(self, conn, floor, fact_id, reviews, review_db, *, contract=LEGACY_CONTRACT,
                          discloses_sources=False):
        snapshot, rows = self._snapshot(conn, floor, fact_id, enforce_floor=True)
        if discloses_sources:
            self._source_sibling_floor(conn, snapshot)
        review = reviews._current_in(review_db, fact_id)
        if review is None:
            raise PolicyError("owner_review_required")
        if review.owner_id != self.binding.owner_id or review.snapshot != snapshot:
            raise PolicyError("review_stale")
        self._eligible(conn, snapshot, rows, review, contract=contract)
        return QualifiedEvidence(family="owner_stated_fact/v1", snapshot=snapshot, review_id=review.review_id,
            review_revision=digest(review.model_dump()), classifications=review.classifications,
            subject_contract=contract, execution_enabled=False), rows

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
                    db.execute("INSERT INTO review_identity VALUES(1,?,?,?,?,?)", (self._binding_json,
                        self.canonical_file_revision, self._clock_id, self._highest_generation, self.store_id))
                else:
                    # Loss of a durable identity/clock is not first enrollment.
                    # Do not rebuild either schema or singleton in existing files.
                    old = db.execute(_IDENTITY_SELECT).fetchone()
                    self._check_identity(old, reopening=True)
                    db.execute("SELECT review_id,fact_id,review_json,active FROM fact_reviews LIMIT 1")
                    db.execute("UPDATE review_identity SET highest_generation=? WHERE singleton=1", (self._highest_generation,))

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
        """Digest of every review row; the observed clock high-water is excluded."""
        rows = db.execute("SELECT review_id,fact_id,review_json,active FROM fact_reviews ORDER BY review_id").fetchall()
        return digest([list(row) for row in rows])

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
                if not initializing:
                    self._check_identity(db.execute(_IDENTITY_SELECT).fetchone())
                    if floor is not None and self._authority_digest(db) != floor.expected_authority_digest():
                        raise PolicyError("review_store_rollback")
                yield db
                self._check_file()
                published = None
                if floor is not None:
                    after = self._authority_digest(db)
                    if after != floor.expected_authority_digest():
                        # Durable before the commit. A crash in either order
                        # leaves the enrollment pending, never a silent reset.
                        floor.publish_pending(after)
                        published = after
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
                db.execute("UPDATE fact_reviews SET active=0 WHERE fact_id=?", (current.fact_id,))
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
            changed = db.execute("UPDATE fact_reviews SET active=0 WHERE review_id=?", (review_id,))
            if changed.rowcount != 1:
                raise PolicyError("review_unknown")
            return review if expected_review_revision is not _ANY_REVIEW else None

    @staticmethod
    def _current_in(db, fact_id):
        rows = db.execute("SELECT review_json FROM fact_reviews WHERE fact_id=? AND active=1", (fact_id,)).fetchmany(2)
        if len(rows) > 1:
            raise PolicyError("review_ambiguous")
        return OwnerEvidenceReview.parse(rows[0][0]) if rows else None

    def _load_current(self, fact_id: str) -> OwnerEvidenceReview:
        with self._db() as db:
            review = self._current_in(db, fact_id)
            if review is None:
                raise PolicyError("owner_review_required")
            return review
