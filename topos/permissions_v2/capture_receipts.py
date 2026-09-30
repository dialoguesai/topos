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
Nothing calls :func:`proven` yet: the evidence layer still accepts only the two
message tables, and this is the rule it will ask when a journal family exists.

The receipt tables sit outside the ``permissions_v2_*`` namespace for the reason
``ai_chat_capture`` gives: the protection clock owns every trigger named that way.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
import uuid
from typing import Any, Optional

from .ai_chat_capture import _columns, _text, install_dataset
from .canonical import PolicyError, digest

VERSION = "topos-capture-attestation/v1"
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


FAMILIES = {
    "journal_entries": Family(
        table="journal_entries", id_column="entry_id", revision_columns=("source_id", "content"),
        statement="These journal entries are my own writing, written through my own app's install on this node."),
}


def family_of(table: Any) -> Family:
    found = FAMILIES.get(table) if isinstance(table, str) else None
    if found is None:
        raise PolicyError("capture_attestation_invalid")
    return found


def content_revision(table: str, row: dict) -> str:
    """The row as the owner attests it: its table, id, source and exact words."""
    family = family_of(table)
    return digest({"table": family.table, "record_id": row.get(family.id_column),
                   **{column: row.get(column) for column in family.revision_columns}})


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


def proven(conn, *, owner_id: str, table: str, identity_source_id: Any, row: dict) -> bool:
    """True only for a row this owner's own door wrote, or one the owner attested (see the module docstring)."""
    from ..features.provenance.writer_class import WRITER_OWNER_APP, WRITER_OWNER_IMPORT, normalize_writer_class

    family = FAMILIES.get(table) if isinstance(table, str) else None
    source_id = row.get("source_id")
    if (family is None or not _text(owner_id) or not _text(source_id) or source_id != identity_source_id
            or not isinstance(row.get(family.id_column), str) or not row[family.id_column]):
        return False
    dataset = install_dataset(conn, owner_id=owner_id, source_id=source_id)
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


# --- the owner's one-time attestation of pre-stamp rows ------------------------------------

def eligible_rows(conn, *, owner_id: str, table: str, source_id: str) -> list:
    """(record_id, content_revision) of every pre-stamp row of this source an attestation would cover.

    A pre-stamp row has no writer class recorded. Rows a live receipt already lists at their current
    revision are left out. Nothing is eligible while the source's install does not bind it to this
    owner and one dataset: that binding is what a receipt certifies.
    """
    family = family_of(table)
    found = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (family.table,)).fetchmany(2)
    if len(found) != 1 or found[0][0] != "table":
        return []
    columns = _columns(conn, family.table)
    if not {family.id_column, "writer_class", *family.revision_columns} <= columns:
        return []
    if install_dataset(conn, owner_id=owner_id, source_id=source_id) is None:
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


def _check_request(owner_id: Any, table: Any, source_id: Any, app_id: Any) -> tuple:
    family = family_of(table)
    owner, source, app = _text(owner_id), _text(source_id), _text(app_id)
    if owner is None:
        raise PolicyError("owner_authority_required")
    if source is None or app is None:
        raise PolicyError("capture_attestation_invalid")
    return owner, family, source, app


def _summary(owner: str, family: Family, source: str, app: str, rows: list, dataset: Optional[str]) -> dict:
    return {"version": VERSION, "table": family.table, "source_id": source, "app_id": app, "row_count": len(rows),
            "statement": family.statement, "dataset_certified": dataset is not None,
            "preview_digest": digest({"version": VERSION, "owner_id": owner, "table": family.table,
                                      "source_id": source, "app_id": app, "dataset_id": dataset,
                                      "rows": [list(r) for r in rows]})}


def preview(conn, *, owner_id: str, table: str, source_id: str, app_id: str) -> dict:
    """Counts, whether the install certifies a dataset, and the digest the owner confirms. No ids, no content."""
    owner, family, source, app = _check_request(owner_id, table, source_id, app_id)
    return _summary(owner, family, source, app,
                    eligible_rows(conn, owner_id=owner, table=family.table, source_id=source),
                    install_dataset(conn, owner_id=owner, source_id=source))


def attest(conn, *, owner_id: str, table: str, source_id: str, app_id: str, preview_digest: Any, confirm: Any,
           now: Optional[int] = None) -> dict:
    """Record the owner's receipt for exactly the rows the confirmed preview named. The caller commits.

    Refused while the source's install certifies no dataset: a receipt that binds nothing to this owner
    would prove nothing. A receipt over zero rows is allowed: it attests the app for the rows it writes next.
    """
    owner, family, source, app = _check_request(owner_id, table, source_id, app_id)
    if confirm is not True:
        raise PolicyError("capture_attestation_unconfirmed")
    dataset = install_dataset(conn, owner_id=owner, source_id=source)
    rows = eligible_rows(conn, owner_id=owner, table=family.table, source_id=source)
    summary = _summary(owner, family, source, app, rows, dataset)
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
                 (receipt_id, VERSION, owner, family.table, source, app, family.statement,
                  summary["preview_digest"], len(rows), attested_at, dataset))
    conn.executemany(f"INSERT INTO {RECEIPT_ROWS} (receipt_id, canonical_table, record_id, content_revision) "
                     "VALUES (?,?,?,?)", [(receipt_id, family.table, *row) for row in rows])
    return {"receipt_id": receipt_id, "version": VERSION, "table": family.table, "source_id": source, "app_id": app,
            "row_count": len(rows), "dataset_certified": True, "preview_digest": summary["preview_digest"],
            "attested_at": attested_at}


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
