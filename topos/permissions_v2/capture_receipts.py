"""Owner-written rows of canonical tables other than AI chat: who wrote a row, proven (OD-50, OD-52).

``features/provenance/roles.py`` calls a journal row authored by construction: the
table is the owner's writing, so the row is. That is a property of the table's
name, not of the row. Any writer that reaches an ingest door of a journal-lane
source can put a row there, and a row written before the node recorded writer
classes (every journal row on a node installed before September 2026) says
nothing about its door. For a grant, "the owner wrote this" has to be a recorded
fact. This module is OD-39's rule (``ai_chat_capture.py``) for the other tables:

A row of a registered table is the owner's own (:func:`proven`) only when

- the evidence identity names the row's own source, and that source has exactly
  one live install on this node, scoped to this owner and to one concrete
  dataset (``ai_chat_capture.install_dataset``). These tables carry no owner and
  no dataset column, so the install is what binds a row to the owner;
- and either the door recorded an owner writer for it: ``owner_app`` whose
  verified stamp names an app this owner attested for this table and source, or
  ``owner_import`` (the owner's own file import), in both cases written into the
  install's dataset (``writer_dataset_id``, recorded by the door, never taken
  from the payload);
- or, for a row written before writer classes were recorded (NULL), a live owner
  receipt lists the row at its current content revision.

Everything else fails: a relay write without a stamp (``cp_relay``), a third
party, the owner's automation, the shared legacy key, an owner write from an app
the owner never attested, an owner-socket write that names no app, and a row of
a source with two live installs or none.

Pre-stamp rows are never re-labelled. The owner attests them once
(``topos/api/permissions_capture_receipts.py``, owner socket only): a preview
names the rows by count and digest, and the owner confirms that exact digest.
The receipt records each row's content revision, so a later rewrite falls out of
it; a receipt can be revoked, never edited. A receipt also names the capture app
the owner vouches for, which is what lets that app's later stamped writes count.

This module decides provenance only. Off-limits, special categories, consent,
revocation, copies, time and every other check run where they run for messages.
Nothing calls :func:`proven` for journals yet: the evidence layer still accepts
only the two message tables, and this is the rule it will ask when a journal
family exists.

AI chat's file-import lane uses the same receipts (IF-5 §1, ``ai_message``: "export
lane or OD-39 capture"). A ChatGPT export imported through ``chatgpt_file_ingestion``
is neither the signed export lane (``chatgpt-owner-snapshot``, which keeps its own
proof) nor a capture app, and its pre-stamp rows (14,408 on one owner's node) carry
no writer and no dataset. ``ai_chat_capture.capture_proven`` asks :func:`proven`
for a user-role row of such a source, and ``ai_chat_capture.certified_dataset``
asks :func:`attested_datasets` for any row of one, so the owner's one receipt over
the import is what binds it to the owner and to the install's dataset. Only a
bundled AI-chat file-import source is admitted (:func:`ai_chat_export_source`), and
the receipt's app is the import door itself (``owner_import``): an export is
imported, never captured, so no app's stamp can stand in for it.

An export receipt may also NAME its dataset (:data:`NAMED_VERSION`). The install record
certifies a dataset only by elimination (``install_dataset``: one live install, one
dataset ever), and one owner's export source has two live installs on two datasets
(31 Aug, on this node, declaring ``mixed``; 9 Sep, on another node's topos, declaring
none, and written after the export's last pre-stamp row). Retiring one moves the ingest
source clock, which stales every native proof and forces a refresh that deletes old
links, and still leaves two datasets in the history. Instead the owner names, in the
preview and the attestation, the dataset of one of the source's own live installs
(:func:`named_install`: exactly one active install bound to it, scoped to this owner
and to this node's own topos, live, declaring its posture; never an arbitrary id, a
retired install, another source's, another owner's or another node's). The receipt
lists the rows at their current revisions as any receipt does, and :func:`named_dataset`
names the dataset for a listed row. On every read ``evidence._source_posture`` then
resolves that row's posture from the named install alone (an install scoped to another
concrete dataset is that dataset's and is set aside; without exactly one install on the
named dataset, declaring its posture, it refuses; an ambient override anywhere on the
source still vetoes), and :func:`proven` accepts the row where no install binds the
source by elimination, while the named install carries it. No install row is written.
Rows the receipt does not list, door-stamped rows and the journal and browser families
are unchanged.

The receipt tables sit outside the ``permissions_v2_*`` namespace for the reason
``ai_chat_capture`` gives: the protection clock owns every trigger named that way.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
import uuid
from typing import Any, Callable, Optional

from .ai_chat_capture import INSTALL_LIVE, _columns, _text, install_dataset
from .canonical import PolicyError, Rows, digest, digest_stream

VERSION = "topos-capture-attestation/v1"
#: A receipt that names the dataset of the install its rows came in through (a family with a ``named_statement``).
NAMED_VERSION = "topos-capture-attestation/named-dataset/v1"
#: What a named install must declare (``evidence._source_posture``'s postures): no default stands in for it.
POSTURES = frozenset({"personal", "mixed", "ambient"})
RECEIPTS = "capture_receipts"
RECEIPT_ROWS = "capture_receipt_rows"


@dataclass(frozen=True)
class Family:
    """One canonical table whose rows an owner can attest. Table and column names are fixed here, never supplied."""
    table: str
    id_column: str
    #: What the owner attests for one row, beside the table and the row's id: its source and its exact words.
    revision_columns: tuple
    statement: str
    #: Revision columns that enter the revision as the SHA-256 of their UTF-8 bytes. Still exact (any edit
    #: moves the revision); it keeps one long or unencodable row from refusing a whole preview.
    hashed_columns: tuple = ()
    #: The only app a receipt of this family may name; None = any app the owner vouches for.
    app_ids: Optional[frozenset] = None
    #: Which sources a receipt of this family may cover; None = any.
    admits: Optional[Callable[[Any], bool]] = None
    #: What the owner attests when the receipt names its install's dataset; None = this family never names one.
    named_statement: Optional[str] = None


def ai_chat_export_source(source_id: Any) -> bool:
    """A bundled AI-chat source whose rows arrive only as the owner's file upload (the export import lane).

    Not the signed export lane (``chatgpt-owner-snapshot`` has its own proof and no bundled definition), and not
    a capture source (``client_push``): OD-39's receipts cover those.
    """
    from topos.sources.registry import BUNDLED_REGISTRY
    from .ingest_protocol import CHATGPT_SOURCE_ID

    if _text(source_id) is None or source_id == CHATGPT_SOURCE_ID:
        return False
    source = BUNDLED_REGISTRY.get(source_id)
    return (source is not None and getattr(source, "canonical_group_id", None) == "ai_messages"
            and getattr(source, "delivery", None) == "owner_upload")


FAMILIES = {
    "journal_entries": Family(
        table="journal_entries", id_column="entry_id", revision_columns=("source_id", "content"),
        statement="These journal entries are my own writing, written through my own app's install on this node."),
    # Browser visits (OD-52 P7). A visit is the owner's activity, never the owner's words: proof here only lets
    # it count toward a derived interest (interest_family.py); no visit is ever released. A visit's revision is
    # its source, url and time (the design's content revision for the family), not its title: the page's title
    # is the site's text, and the owner vouches for having visited, not for what the page said.
    "activity_events": Family(
        table="activity_events", id_column="event_id", revision_columns=("source_id", "url", "occurred_at"),
        statement="These browser visits are my own browsing, captured by my own browser plugin's install on this node."),
    "ai_chat_messages": Family(
        table="ai_chat_messages", id_column="message_id",
        revision_columns=("source_id", "conversation_id", "sender_type", "content"), hashed_columns=("content",),
        statement=("These AI-chat rows came from my own export, imported through this source's install on this "
                   "node; the prompts in it are my own words."),
        named_statement=("These AI-chat rows came from my own export, imported through the install of this source "
                         "on this node that is bound to the dataset I name; the prompts in it are my own words."),
        app_ids=frozenset({"owner_import"}), admits=ai_chat_export_source),
}


def family_of(table: Any) -> Family:
    found = FAMILIES.get(table) if isinstance(table, str) else None
    if found is None:
        raise PolicyError("capture_attestation_invalid")
    return found


def _hashed(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def content_revision(table: str, row: dict) -> str:
    """The row as the owner attests it: its table, id, source and exact words."""
    family = family_of(table)
    return digest({"table": family.table, "record_id": row.get(family.id_column),
                   **{column: _hashed(row.get(column)) if column in family.hashed_columns else row.get(column)
                      for column in family.revision_columns}})


def installed(conn) -> bool:
    return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (?,?)",
                        (RECEIPTS, RECEIPT_ROWS)).fetchone()[0] == 2


def install(conn) -> None:
    """Create the receipt tables. Receipts are append-only: a row list never changes, a receipt only gets revoked."""
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {RECEIPTS} (
        receipt_id TEXT PRIMARY KEY, version TEXT NOT NULL, owner_id TEXT NOT NULL, canonical_table TEXT NOT NULL,
        source_id TEXT NOT NULL, app_id TEXT NOT NULL, statement TEXT NOT NULL, preview_digest TEXT NOT NULL,
        row_count INTEGER NOT NULL, attested_at INTEGER NOT NULL, revoked_at INTEGER, dataset_id TEXT NOT NULL)""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {RECEIPT_ROWS} (
        receipt_id TEXT NOT NULL, canonical_table TEXT NOT NULL, record_id TEXT NOT NULL,
        content_revision TEXT NOT NULL, PRIMARY KEY (receipt_id, record_id))""")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{RECEIPT_ROWS}_record ON {RECEIPT_ROWS}(canonical_table, record_id)")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {RECEIPTS}_immutable BEFORE UPDATE OF
        receipt_id, version, owner_id, canonical_table, source_id, app_id, statement, preview_digest, row_count,
        attested_at, dataset_id ON {RECEIPTS} BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {RECEIPTS}_revoke_once BEFORE UPDATE OF revoked_at ON {RECEIPTS}
        WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
        BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {RECEIPT_ROWS}_immutable BEFORE UPDATE ON {RECEIPT_ROWS}
        BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {RECEIPT_ROWS}_live_receipt BEFORE INSERT ON {RECEIPT_ROWS}
        WHEN (SELECT COUNT(*) FROM {RECEIPTS} WHERE receipt_id=NEW.receipt_id AND revoked_at IS NULL
              AND canonical_table=NEW.canonical_table) != 1
        BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END""")


def capture_apps(conn, *, owner_id: str, table: str, source_id: str) -> frozenset:
    """The apps this owner attested for this table and source, from live receipts. Another owner's never count."""
    if not _text(owner_id) or not _text(source_id) or not installed(conn):
        return frozenset()
    return frozenset(r[0] for r in conn.execute(
        f"SELECT app_id FROM {RECEIPTS} WHERE owner_id=? AND canonical_table=? AND source_id=? AND revoked_at IS NULL",
        (owner_id, family_of(table).table, source_id)))


def attested_revisions(conn, *, owner_id: str, table: str, source_id: str, record_id: Any) -> frozenset:
    """Content revisions of this row that a live receipt of this owner, for this table and source, lists."""
    if not installed(conn) or not isinstance(record_id, str):
        return frozenset()
    return frozenset(r[0] for r in conn.execute(
        f"SELECT r.content_revision FROM {RECEIPT_ROWS} r JOIN {RECEIPTS} t ON t.receipt_id=r.receipt_id "
        "WHERE r.canonical_table=? AND r.record_id=? AND t.owner_id=? AND t.canonical_table=? AND t.source_id=? "
        "AND t.revoked_at IS NULL", (table, record_id, owner_id, table, source_id)))


def attested_datasets(conn, *, owner_id: str, table: str, row: dict) -> frozenset:
    """Datasets of this owner's live receipts that list this row at its current revision (empty for a stamped row).

    For a caller that combines them with another family's receipts: ``ai_chat_capture.certified_dataset`` joins
    them to OD-39's, and one dataset across both is the only answer that certifies.
    """
    from ..features.provenance.writer_class import normalize_writer_class

    family = family_of(table)
    source_id = row.get("source_id")
    if (_text(owner_id) is None or _text(source_id) is None or normalize_writer_class(row.get("writer_class"))
            is not None or not installed(conn) or not isinstance(row.get(family.id_column), str)):
        return frozenset()
    return frozenset(r[0] for r in conn.execute(
        f"SELECT t.dataset_id FROM {RECEIPT_ROWS} r JOIN {RECEIPTS} t ON t.receipt_id=r.receipt_id "
        "WHERE r.canonical_table=? AND r.record_id=? AND r.content_revision=? AND t.owner_id=? "
        "AND t.canonical_table=? AND t.source_id=? AND t.revoked_at IS NULL",
        (family.table, row[family.id_column], content_revision(table, row), owner_id, family.table, source_id)))


def certified_dataset(conn, *, owner_id: str, table: str, row: dict) -> Optional[str]:
    """The dataset this row was written through, from the node's own record, or None.

    A door-written row names it in ``writer_dataset_id``. A pre-stamp row has only this owner's live
    receipts at its current revision, and they must agree on one dataset. Used to resolve the posture
    of a source whose install is scoped to a dataset the row itself does not carry (RD5).
    """
    from ..features.provenance.writer_class import normalize_writer_class

    family = family_of(table)
    source_id = row.get("source_id")
    if _text(owner_id) is None or _text(source_id) is None:
        return None
    if normalize_writer_class(row.get("writer_class")) is not None:
        return _text(row.get("writer_dataset_id"))
    if not installed(conn) or not isinstance(row.get(family.id_column), str):
        return None
    found = frozenset(r[0] for r in conn.execute(
        f"SELECT t.dataset_id FROM {RECEIPT_ROWS} r JOIN {RECEIPTS} t ON t.receipt_id=r.receipt_id "
        "WHERE r.canonical_table=? AND r.record_id=? AND r.content_revision=? AND t.owner_id=? "
        "AND t.canonical_table=? AND t.source_id=? AND t.revoked_at IS NULL",
        (family.table, row[family.id_column], content_revision(table, row), owner_id, family.table, source_id)))
    return _text(next(iter(found))) if len(found) == 1 else None


def _parsed(text: Any) -> Optional[dict]:
    """A JSON object read strictly (a repeated key is unreadable, as ``evidence._json`` reads it), else None."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(key)
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs) if isinstance(text, str) else None
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def named_install(conn, *, owner_id: Any, source_id: Any, dataset_id: Any) -> Optional[dict]:
    """The one live install of this source for this owner that a receipt may name by its dataset, or None.

    Every active install of the source is read. One scoped to another concrete dataset is that dataset's install
    and is set aside; every other one (on this dataset, a wildcard, an unscoped or unreadable one) could carry
    this dataset's rows, so exactly one may remain. Its scope must read as ``evidence._source_posture`` reads it
    (known fields, each a plain string, any device), name exactly this concrete dataset and this owner (or every
    owner); it must be live and name this source. It comes back with its scope and its declared posture, which
    every caller requires to be one of :data:`POSTURES`: a receipt never lets a default stand in for a
    declaration. Whose node the install is scoped to is the caller's check (:func:`_naming_refusal` against the
    node's own topos at attestation; the evidence binding in ``_source_posture`` on every read).
    """
    owner, source, dataset = _text(owner_id), _text(source_id), _text(dataset_id)
    if owner is None or source is None or dataset is None or dataset == "*":
        return None
    found = conn.execute("SELECT type FROM sqlite_master WHERE name='source_runtime_installs'").fetchmany(2)
    if len(found) != 1 or found[0][0] != "table":
        return None
    columns = _columns(conn, "source_runtime_installs")
    if not {"install_id", "source_id", "scope_key", "is_active", "status", "source_definition_json"} <= columns:
        return None
    created = "created_at" if "created_at" in columns else "NULL"
    kept = []
    for values in conn.execute(f"SELECT install_id, scope_key, is_active, status, source_definition_json, {created} "
                               "FROM source_runtime_installs WHERE source_id=? AND is_active IS NOT 0", (source,)):
        scope = _parsed(values[1])
        bound = scope.get("dataset_id") if scope is not None else None
        if isinstance(bound, str) and bound and bound == bound.strip() and bound not in ("*", dataset):
            continue
        kept.append((values, scope))
    if len(kept) != 1:
        return None
    (install_id, _scope_key, is_active, status, definition, created_at), scope = kept[0]
    parsed = _parsed(definition)
    if (scope is None or not set(scope) <= {"user_id", "device_id", "topos_id", "app_id", "dataset_id"}
            or any(not isinstance(value, str) or not value or value != value.strip() for value in scope.values())
            or scope.get("dataset_id") != dataset or scope.get("user_id") not in (owner, "*")
            or scope.get("device_id", "*") != "*"
            or type(is_active) is not int or is_active != 1 or status not in INSTALL_LIVE
            or parsed is None or parsed.get("source_id", source) != source):
        return None
    posture = parsed.get("posture")
    return {"install_id": install_id, "created_at": created_at, "dataset_id": dataset, "scope": scope,
            "declared_posture": posture if isinstance(posture, str) else None}


def _naming_refusal(conn, *, owner_id: str, source_id: str, dataset_id: str, resource_id: Any) -> Optional[str]:
    """Why a receipt may not name this dataset (a refusal code), or None when it may.

    The install must be :func:`named_install`'s, scoped to this node's own topos (``resource_id``, the node
    identity's resource id; never another node's or device's install, never a wildcard), and declare its posture.
    """
    found = named_install(conn, owner_id=owner_id, source_id=source_id, dataset_id=dataset_id)
    if found is None:
        return "capture_attestation_dataset_unknown"
    node = _text(resource_id)
    places = [found["scope"][field] for field in ("topos_id", "app_id") if field in found["scope"]]
    if node is None or not places or any(place != node for place in places):
        return "capture_attestation_dataset_not_this_node"
    if found["declared_posture"] not in POSTURES:
        return "capture_attestation_dataset_posture_unknown"
    return None


def named_dataset(conn, *, owner_id: str, table: str, row: dict) -> Optional[str]:
    """The dataset the owner's receipt names for this pre-stamp row, or None.

    A live receipt of this owner that names its dataset lists the row at its current revision, and every live
    receipt of the family that lists it there names that one dataset (two datasets are ambiguous and certify
    nothing). A stamped row, a family that never names a dataset and a source the family does not admit get None.
    Whether the named install still carries the row is asked on every read, by each reader: posture resolves only
    from that install (``evidence._source_posture``, which refuses without it and checks its scope against the
    evidence binding, this node's topos included), and :func:`proven` asks :func:`named_install`.
    """
    from ..features.provenance.writer_class import normalize_writer_class

    family = FAMILIES.get(table) if isinstance(table, str) else None
    source_id = row.get("source_id")
    if (family is None or family.named_statement is None or _text(owner_id) is None or _text(source_id) is None
            or (family.admits is not None and not family.admits(source_id))
            or normalize_writer_class(row.get("writer_class")) is not None or not installed(conn)
            or not isinstance(row.get(family.id_column), str) or not row[family.id_column]):
        return None
    listed = conn.execute(
        f"SELECT t.dataset_id, t.version FROM {RECEIPT_ROWS} r JOIN {RECEIPTS} t ON t.receipt_id=r.receipt_id "
        "WHERE r.canonical_table=? AND r.record_id=? AND r.content_revision=? AND t.owner_id=? "
        "AND t.canonical_table=? AND t.source_id=? AND t.revoked_at IS NULL",
        (family.table, row[family.id_column], content_revision(table, row), owner_id, family.table,
         source_id)).fetchall()
    datasets = {dataset for dataset, _version in listed}
    if len(datasets) != 1 or NAMED_VERSION not in {version for _dataset, version in listed}:
        return None
    return _text(next(iter(datasets)))


def candidates(conn, *, owner_id: str, table: str, source_id: str, resource_id: Any) -> list:
    """The source's active installs a receipt over it could name, for the owner's preview. No row, no content.

    One entry per active install scoped to a concrete dataset of this owner (or every owner): its id, date,
    dataset and declared posture; why a receipt may not name it (``refusal``, the code naming it would get; None
    when it may); and how many rows a receipt naming it would cover (0 when it may not be named).
    """
    family = family_of(table)
    found = conn.execute("SELECT type FROM sqlite_master WHERE name='source_runtime_installs'").fetchmany(2)
    if family.named_statement is None or len(found) != 1 or found[0][0] != "table":
        return []
    columns = _columns(conn, "source_runtime_installs")
    if not {"install_id", "source_id", "scope_key", "is_active", "source_definition_json"} <= columns:
        return []
    created = "created_at" if "created_at" in columns else "NULL"
    out, covered = [], None
    for install_id, scope_key, definition, created_at in conn.execute(
            f"SELECT install_id, scope_key, source_definition_json, {created} FROM source_runtime_installs "
            f"WHERE source_id=? AND is_active IS NOT 0 ORDER BY {created}, install_id", (source_id,)):
        scope, parsed = _parsed(scope_key), _parsed(definition)
        dataset = scope.get("dataset_id") if scope is not None else None
        if scope is None or scope.get("user_id") not in (owner_id, "*") or _text(dataset) is None or dataset == "*":
            continue
        refusal = _naming_refusal(conn, owner_id=owner_id, source_id=source_id, dataset_id=dataset,
                                  resource_id=resource_id)
        if refusal is None and covered is None:
            # Which install is named never changes which rows are eligible, only whether any are.
            covered = len(eligible_rows(conn, owner_id=owner_id, table=family.table, source_id=source_id,
                                        dataset_id=dataset))
        posture = parsed.get("posture") if parsed is not None else None
        out.append({"install_id": install_id, "created_at": created_at, "dataset_id": dataset,
                    "declared_posture": posture if isinstance(posture, str) else None,
                    "nameable": refusal is None, "refusal": refusal, "row_count": covered if refusal is None else 0})
    return out


def proven(conn, *, owner_id: str, table: str, identity_source_id: Any, row: dict) -> bool:
    """True only for a row this owner's own door wrote, or one the owner attested (see the module docstring)."""
    from ..features.provenance.writer_class import WRITER_OWNER_APP, WRITER_OWNER_IMPORT, normalize_writer_class

    family = FAMILIES.get(table) if isinstance(table, str) else None
    source_id = row.get("source_id")
    if (family is None or not _text(owner_id) or not _text(source_id) or source_id != identity_source_id
            or not isinstance(row.get(family.id_column), str) or not row[family.id_column]):
        return False
    dataset = install_dataset(conn, owner_id=owner_id, source_id=source_id)
    named = named_dataset(conn, owner_id=owner_id, table=table, row=row) if dataset is None else None
    if named is not None:
        # No install binds the source by elimination; the owner's receipt names the install this row came through,
        # and the row counts only while that install still carries it (the one live install on it, declaring).
        found = named_install(conn, owner_id=owner_id, source_id=source_id, dataset_id=named)
        return found is not None and found["declared_posture"] in POSTURES
    if dataset is None:
        return False
    writer = normalize_writer_class(row.get("writer_class"))
    if writer is None:
        return content_revision(table, row) in attested_revisions(
            conn, owner_id=owner_id, table=table, source_id=source_id, record_id=row[family.id_column])
    if row.get("writer_dataset_id") != dataset:
        return False
    if writer == WRITER_OWNER_IMPORT:
        return True
    if writer == WRITER_OWNER_APP:
        app = _text(row.get("writer_app_id"))
        return app is not None and app in capture_apps(conn, owner_id=owner_id, table=table, source_id=source_id)
    return False


def proven_rows(conn, *, owner_id: str, table: str, source_id: str, rows: list) -> frozenset:
    """The ids of the rows :func:`proven` would accept, for many rows of one source, in three reads.

    The same rule, row for row: a row of another source, a malformed id, no certified install, an
    unattested app, a foreign dataset or a stale receipt each leaves the row out. ``rows`` are dicts with
    the family's id and revision columns and the three writer columns (absent means NULL). Where no install
    binds the source by elimination, only a row the owner's receipt names a dataset for can count, and each such
    row is asked of :func:`proven` itself.
    """
    from ..features.provenance.writer_class import WRITER_OWNER_APP, WRITER_OWNER_IMPORT, normalize_writer_class

    family = FAMILIES.get(table) if isinstance(table, str) else None
    if family is None or not _text(owner_id) or not _text(source_id):
        return frozenset()
    dataset = install_dataset(conn, owner_id=owner_id, source_id=source_id)
    if dataset is None:
        return frozenset(row[family.id_column] for row in rows if row.get("source_id") == source_id
                         and isinstance(row.get(family.id_column), str) and row[family.id_column]
                         and proven(conn, owner_id=owner_id, table=table, identity_source_id=source_id, row=row))
    apps = capture_apps(conn, owner_id=owner_id, table=table, source_id=source_id)
    attested: dict = {}
    if installed(conn):
        for record_id, revision in conn.execute(
                f"SELECT r.record_id, r.content_revision FROM {RECEIPT_ROWS} r JOIN {RECEIPTS} t "
                "ON t.receipt_id=r.receipt_id WHERE r.canonical_table=? AND t.owner_id=? AND t.canonical_table=? "
                "AND t.source_id=? AND t.revoked_at IS NULL", (table, owner_id, table, source_id)):
            attested.setdefault(record_id, set()).add(revision)
    found = set()
    for row in rows:
        record_id = row.get(family.id_column)
        if row.get("source_id") != source_id or not isinstance(record_id, str) or not record_id:
            continue
        writer = normalize_writer_class(row.get("writer_class"))
        if writer is None:
            if content_revision(table, row) in attested.get(record_id, ()):
                found.add(record_id)
        elif row.get("writer_dataset_id") == dataset and (
                writer == WRITER_OWNER_IMPORT
                or (writer == WRITER_OWNER_APP and _text(row.get("writer_app_id")) in apps)):
            found.add(record_id)
    return frozenset(found)


def door_dataset(conn, *, owner_id: Any, source_id: Any, authorised: Any) -> Any:
    """The dataset an ingest door records on a row it writes for this owner's source (``writer_dataset_id``).

    The control plane names the dataset of the resource it authorised (``app_ingest``:
    ``dataset:<owner>:<dataset>:<device>``; a local node's is ``<owner>:default:<device>``). The
    node's install of the source is scoped to the dataset the installing app chose (the web app's,
    with a Topos selected, is ``<owner>:topos:<topos id>``). On one node both name the same store.
    :func:`proven` and ``evidence._source_posture`` bind a row to the install's, so a door that
    recorded the resource's name, where the two differ, would leave every row it writes unprovable
    for a reason that says nothing about who wrote it.

    So when the authorised dataset is this owner's (its owner prefix) and the source's install binds
    it to exactly one dataset for this owner (:func:`install_dataset`, the rule :func:`proven` reads),
    the door records that dataset: the row went through that install. Anything else records the
    authorised dataset unchanged, as before, and a row whose dataset is not the install's still proves
    nothing. This names a dataset only; who wrote the row is the writer class and app, recorded from
    the channel principal, never from here and never from the payload.
    """
    owner, requested = _text(owner_id), _text(authorised)
    if owner is None or requested is None or not requested.startswith(owner + ":") or _text(source_id) is None:
        return authorised
    dataset = install_dataset(conn, owner_id=owner, source_id=source_id)
    return dataset if dataset is not None else authorised


# --- the owner's one-time attestation of pre-stamp rows ------------------------------------

def eligible_rows(conn, *, owner_id: str, table: str, source_id: str, dataset_id: Optional[str] = None) -> list:
    """(record_id, content_revision) of every pre-stamp row of this source an attestation would cover.

    A pre-stamp row has no writer class recorded. Rows a live receipt already lists at their current
    revision are left out. Nothing is eligible while nothing binds the source to this owner and one
    dataset: that binding is what a receipt certifies. Without ``dataset_id`` it is the install record by
    elimination (``install_dataset``); with it, the install the owner names by that dataset
    (:func:`named_install`, declaring its posture), for a family whose receipt may name one. Whether that
    install is this node's is the caller's check (:func:`_naming_refusal`); this lists rows, it binds nothing.
    """
    family = family_of(table)
    found = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (family.table,)).fetchmany(2)
    if len(found) != 1 or found[0][0] != "table":
        return []
    columns = _columns(conn, family.table)
    if not {family.id_column, "writer_class", *family.revision_columns} <= columns:
        return []
    if dataset_id is None:
        if install_dataset(conn, owner_id=owner_id, source_id=source_id) is None:
            return []
    else:
        named = (named_install(conn, owner_id=owner_id, source_id=source_id, dataset_id=dataset_id)
                 if family.named_statement is not None else None)
        if named is None or named["declared_posture"] not in POSTURES:
            return []
    selected = ", ".join((family.id_column, *family.revision_columns))
    rows = conn.execute(f"SELECT {selected} FROM {family.table} WHERE source_id=? AND writer_class IS NULL "
                        f"ORDER BY {family.id_column}", (source_id,)).fetchall()
    out = []
    for values in rows:
        row = dict(zip((family.id_column, *family.revision_columns), values))
        record_id = row[family.id_column]
        if not isinstance(record_id, str) or not record_id:
            continue
        revision = content_revision(table, row)
        if revision in attested_revisions(conn, owner_id=owner_id, table=table, source_id=source_id,
                                          record_id=record_id):
            continue
        out.append((record_id, revision))
    return out


def _check_request(owner_id: Any, table: Any, source_id: Any, app_id: Any, dataset_id: Any = None) -> tuple:
    family = family_of(table)
    owner, source, app = _text(owner_id), _text(source_id), _text(app_id)
    if owner is None:
        raise PolicyError("owner_authority_required")
    if source is None or app is None:
        raise PolicyError("capture_attestation_invalid")
    if (family.admits is not None and not family.admits(source)) or (
            family.app_ids is not None and app not in family.app_ids):
        raise PolicyError("capture_attestation_invalid")
    named = None
    if dataset_id is not None:
        # Only a family whose receipt may name its install's dataset takes one, and only a concrete id.
        named = _text(dataset_id)
        if family.named_statement is None or named is None or named == "*":
            raise PolicyError("capture_attestation_invalid")
    return owner, family, source, app, named


def _named_install(conn, owner: str, source: str, named: str, resource: Any) -> None:
    """Refuse a dataset the owner may not name (:func:`_naming_refusal` says why)."""
    refusal = _naming_refusal(conn, owner_id=owner, source_id=source, dataset_id=named, resource_id=resource)
    if refusal is not None:
        raise PolicyError(refusal)


def _summary(owner: str, family: Family, source: str, app: str, rows: list, dataset: Optional[str], *,
             named: bool = False) -> dict:
    version = NAMED_VERSION if named else VERSION
    return {"version": version, "table": family.table, "source_id": source, "app_id": app, "row_count": len(rows),
            "statement": family.named_statement if named else family.statement,
            "dataset_certified": dataset is not None, **({"dataset_id": dataset} if named else {}),
            # Streamed: the same hex `digest` gives, without its 1 MiB cap (an export's rows exceed it).
            "preview_digest": digest_stream({"version": version, "owner_id": owner, "table": family.table,
                                             "source_id": source, "app_id": app, "dataset_id": dataset,
                                             "rows": Rows(rows)})}


def preview(conn, *, owner_id: str, table: str, source_id: str, app_id: str, dataset_id: Any = None,
            resource_id: Any = None) -> dict:
    """Counts, whether a dataset is certified, and the digest the owner confirms. No row ids, no content.

    ``dataset_id`` names the install the rows came in through, for a family whose receipt may name one (the
    AI-chat import); the preview is refused when that dataset is not one install's this owner may name on this
    node (``resource_id``: the node identity's resource id, its own topos). Such a family's preview also lists
    the source's ``candidates``: the installs, and what naming each would cover or why it may not be named.
    """
    owner, family, source, app, named = _check_request(owner_id, table, source_id, app_id, dataset_id)
    if named is not None:
        _named_install(conn, owner, source, named, resource_id)
        summary = _summary(owner, family, source, app, eligible_rows(
            conn, owner_id=owner, table=family.table, source_id=source, dataset_id=named), named, named=True)
    else:
        summary = _summary(owner, family, source, app,
                           eligible_rows(conn, owner_id=owner, table=family.table, source_id=source),
                           install_dataset(conn, owner_id=owner, source_id=source))
    if family.named_statement is not None:
        summary["candidates"] = candidates(conn, owner_id=owner, table=family.table, source_id=source,
                                           resource_id=resource_id)
    return summary


def attest(conn, *, owner_id: str, table: str, source_id: str, app_id: str, preview_digest: Any, confirm: Any,
           now: Optional[int] = None, dataset_id: Any = None, resource_id: Any = None) -> dict:
    """Record the owner's receipt for exactly the rows the confirmed preview named. The caller commits.

    Refused while nothing binds the source to one dataset (neither the install record by elimination nor, with
    ``dataset_id``, an install the owner may name on this node, ``resource_id``): a receipt that binds nothing to
    this owner would prove nothing. A receipt over zero rows is allowed: it attests the app for the rows it writes
    next. A named receipt's version and statement say it names its dataset, and the digest the owner confirmed
    binds both.
    """
    owner, family, source, app, named = _check_request(owner_id, table, source_id, app_id, dataset_id)
    if confirm is not True:
        raise PolicyError("capture_attestation_unconfirmed")
    if named is not None:
        _named_install(conn, owner, source, named, resource_id)
    dataset = named if named is not None else install_dataset(conn, owner_id=owner, source_id=source)
    rows = eligible_rows(conn, owner_id=owner, table=family.table, source_id=source, dataset_id=named)
    summary = _summary(owner, family, source, app, rows, dataset, named=named is not None)
    if preview_digest != summary["preview_digest"]:
        raise PolicyError("capture_attestation_preview_stale")
    if dataset is None:
        raise PolicyError("capture_attestation_invalid")
    install(conn)
    receipt_id = "cap-" + uuid.uuid4().hex
    attested_at = int(time.time() if now is None else now)
    conn.execute(f"INSERT INTO {RECEIPTS} (receipt_id, version, owner_id, canonical_table, source_id, app_id, "
                 "statement, preview_digest, row_count, attested_at, revoked_at, dataset_id) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)",
                 (receipt_id, summary["version"], owner, family.table, source, app, summary["statement"],
                  summary["preview_digest"], len(rows), attested_at, dataset))
    conn.executemany(f"INSERT INTO {RECEIPT_ROWS} (receipt_id, canonical_table, record_id, content_revision) "
                     "VALUES (?,?,?,?)", [(receipt_id, family.table, *row) for row in rows])
    return {"receipt_id": receipt_id, "version": summary["version"], "table": family.table, "source_id": source,
            "app_id": app, "row_count": len(rows), "dataset_certified": True,
            **({"dataset_id": dataset} if named is not None else {}),
            "preview_digest": summary["preview_digest"], "attested_at": attested_at}


def revoke(conn, *, owner_id: str, receipt_id: Any, now: Optional[int] = None) -> dict:
    """Withdraw a receipt: its rows and its app stop counting. The caller commits."""
    if not _text(owner_id):
        raise PolicyError("owner_authority_required")
    if not installed(conn) or not _text(receipt_id):
        raise PolicyError("capture_receipt_unknown")
    found = conn.execute(f"SELECT revoked_at FROM {RECEIPTS} WHERE receipt_id=? AND owner_id=?",
                         (receipt_id, owner_id)).fetchone()
    if found is None:
        raise PolicyError("capture_receipt_unknown")
    if found[0] is not None:
        raise PolicyError("capture_receipt_revoked")
    revoked_at = int(time.time() if now is None else now)
    conn.execute(f"UPDATE {RECEIPTS} SET revoked_at=? WHERE receipt_id=?", (revoked_at, receipt_id))
    return {"receipt_id": receipt_id, "revoked_at": revoked_at}


def receipts(conn, *, owner_id: str) -> list:
    """This owner's receipts, newest first: ids, counts and times only."""
    if not installed(conn):
        return []
    fields = ("receipt_id", "version", "canonical_table", "source_id", "app_id", "row_count", "preview_digest",
              "attested_at", "revoked_at")
    out = []
    for row in conn.execute(f"SELECT {', '.join(fields)} FROM {RECEIPTS} WHERE owner_id=? "
                            "ORDER BY attested_at DESC, receipt_id", (owner_id,)):
        item = dict(zip(fields, row))
        item["table"] = item.pop("canonical_table")
        out.append(item)
    return out
