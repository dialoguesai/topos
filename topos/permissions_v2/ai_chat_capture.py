"""AI-chat capture provenance: the owner's own capture app's prompts are the owner's words (OD-39).

Owner decision OD-39 (29 Sep 2026): rows captured by the owner's ChatGPT browser
extension count as the owner's own words. A user-role row is owner-authored; an
assistant row stays an AI reply. The same holds for any owner who attaches their
own AI-chat capture source to the AI-chat canonical tables. It supersedes the
fresh-export requirement of OD-16b for these rows (the export lane in
``ingest_protocol`` / ``chatgpt_owner_snapshot`` keeps its own proof).

``sender_type`` proves nothing on its own: any writer that reaches app_ingest can
mint a "human" row, and a conversation's owner is only a dataset id prefix. What
separates the owner's capture from forgery is WHO WROTE the row, which the node
records from the channel principal and never from the payload
(``features/provenance/writer_class.py``). A capture row is the owner's words
only when all of these hold (:func:`capture_proven`):

- it is a user-role row of a source on the owner's capture list
  (:func:`capture_sources`: OD-39's ChatGPT extension source, plus sources the
  owner attested themselves), and the evidence identity names the same source;
- its one parent conversation carries that source and is bound to this owner;
- its writer is an owner class through the capture: ``owner_app`` whose
  verified stamp names one of that source's capture apps (the CP stamps
  ``app_ingest`` ``owner_app`` only when requester == owner AND the app is an
  attested capture app, ``control_plane/owner_write_stamp.py`` rule C), or
  ``owner_import``;
- or, for a row written before writer classes were recorded (NULL), a live
  owner attestation receipt lists the row at its current content revision.

A grantee's write (``cp_relay``), a third party, the owner's automation, the
owner's socket without a capture app, and an owner write from any other app
never pass. ``local_legacy`` does not either: the shared key is not a capture.

Pre-stamp rows are never re-labelled. The owner runs a one-time attestation
(``topos/api/permissions_ai_chat_capture.py``, owner socket only): a preview
names the rows by count and digest, and the owner confirms that exact digest.
The receipt records each row's content revision, so a later rewrite of a row
falls out of it; a receipt can be revoked, never edited.

RD5 adds where a row came in. An AI-chat evidence identity carries no dataset,
so a source whose install is scoped to one dataset could never resolve its
posture. :func:`certified_dataset` names the dataset a row was written through,
from the node's own record of the write and never from the row's payload:

- a row written by a door carries ``writer_dataset_id``, the dataset that door
  wrote it into (``app_ingest``: the CP-authorised resource's dataset), and it
  moves with ``writer_class`` exactly as ``writer_app_id`` does;
- a pre-stamp row (no writer recorded) has no such record, so only a live owner
  receipt listing it at its current revision can name one, and only the dataset
  :func:`install_dataset` finds: the one dataset every recorded install of that
  source for this owner is scoped to. Two datasets, or none, certify nothing.

Anything else stays uncertified, and ``evidence._source_posture`` treats it as
before: a dataset-scoped install then leaves its posture unknown.

The export import lane (``chatgpt_file_ingestion`` and any other bundled AI-chat
file-upload source) is neither a capture app nor the signed export lane. Its
pre-stamp rows are proven and certified by the owner's receipt over the import in
the generalised receipts (``capture_receipts``, table ``ai_chat_messages``): a
user-role row whose parent is bound to the owner is the owner's words when the
door recorded ``owner_import`` into the install's dataset, or when a live receipt
lists it at its current revision (:func:`capture_proven`); any row such a receipt
lists certifies the receipt's dataset (:func:`certified_dataset`). An app's stamp
never proves an export row.

This module decides provenance only. Off-limits, special categories, consent,
revocation, copies and every other check stay where they are and run as before.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Optional

from .canonical import PolicyError, digest

VERSION = "topos-ai-chat-capture-attestation/v2"
STATEMENT = ("These AI-chat rows were captured from my own conversations by my own capture app, "
             "through its install on this node.")
# Deliberately outside the ``permissions_v2_*`` namespace: the protection clock owns every trigger
# named that way (``protection_clock.clock_state``), and a foreign one would take every read down.
RECEIPTS = "ai_chat_capture_receipts"
RECEIPT_ROWS = "ai_chat_capture_receipt_rows"
USER_ROLES = ("human", "user")
#: Install states ``evidence._source_posture`` accepts as live.
INSTALL_LIVE = ("installed", "active", "ready")
MAX_ID = 128

#: OD-39: the ChatGPT browser extension's source and the app id the CP stamps it
#: under (``OWNER_CAPTURE_APP_IDS``, default ``chatgpt-shadow-extension``).
OD39_SOURCE_ID = "chatgpt_ui_conversation"
OD39_APP_IDS = ("chatgpt-shadow-extension",)
#: Mirrors the CP's ``OWNER_CAPTURE_APP_IDS`` for the OD-39 source when the CP's is changed. It takes
#: the CP's entries as they are: a bare app id, or ``app_id:source_id``, of which only this source's
#: count here (another source's capture app is not this source's).
APP_IDS_ENV = "TOPOS_OWNER_CAPTURE_APP_IDS"


def _od39_app_ids() -> frozenset:
    raw = os.environ.get(APP_IDS_ENV)
    if raw is None:
        return frozenset(OD39_APP_IDS)
    apps = set()
    for part in raw.split(","):
        app, colon, source = (piece.strip() for piece in part.partition(":"))
        if app and (not colon or source == OD39_SOURCE_ID):
            apps.add(app)
    return frozenset(apps)


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value and value == value.strip() and len(value) <= MAX_ID else None


def content_revision(row: dict) -> str:
    """What the owner attests for one row: its identity, speaker and exact words."""
    return digest({"message_id": row.get("message_id"), "conversation_id": row.get("conversation_id"),
                   "source_id": row.get("source_id"), "sender_type": row.get("sender_type"),
                   "content": row.get("content")})


def installed(conn) -> bool:
    return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (?,?)",
                        (RECEIPTS, RECEIPT_ROWS)).fetchone()[0] == 2


def install(conn) -> None:
    """Create the receipt tables. Receipts are append-only: a row list never changes, a receipt only gets revoked."""
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {RECEIPTS} (
        receipt_id TEXT PRIMARY KEY, version TEXT NOT NULL, owner_id TEXT NOT NULL, source_id TEXT NOT NULL,
        app_id TEXT NOT NULL, statement TEXT NOT NULL, preview_digest TEXT NOT NULL, row_count INTEGER NOT NULL,
        attested_at INTEGER NOT NULL, revoked_at INTEGER, dataset_id TEXT)""")
    if "dataset_id" not in _columns(conn, RECEIPTS):
        # A v1 table (RD5 adds the dataset): its rows keep NULL, which certifies no dataset.
        conn.execute(f"ALTER TABLE {RECEIPTS} ADD COLUMN dataset_id TEXT")
        conn.execute(f"DROP TRIGGER IF EXISTS {RECEIPTS}_immutable")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {RECEIPT_ROWS} (
        receipt_id TEXT NOT NULL, message_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
        content_revision TEXT NOT NULL, PRIMARY KEY (receipt_id, message_id))""")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{RECEIPT_ROWS}_message ON {RECEIPT_ROWS}(message_id)")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {RECEIPTS}_immutable BEFORE UPDATE OF
        receipt_id, version, owner_id, source_id, app_id, statement, preview_digest, row_count, attested_at, dataset_id
        ON {RECEIPTS} BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {RECEIPTS}_revoke_once BEFORE UPDATE OF revoked_at ON {RECEIPTS}
        WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
        BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {RECEIPT_ROWS}_immutable BEFORE UPDATE ON {RECEIPT_ROWS}
        BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {RECEIPT_ROWS}_live_receipt BEFORE INSERT ON {RECEIPT_ROWS}
        WHEN (SELECT COUNT(*) FROM {RECEIPTS} WHERE receipt_id=NEW.receipt_id AND revoked_at IS NULL) != 1
        BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END""")


def capture_sources(conn, owner_id: str) -> dict:
    """source_id -> the capture app ids this owner's rows may come through.

    OD-39's ChatGPT extension source for every owner, plus each (source, app) the
    owner attested in a live receipt. Another owner's receipts never count.
    """
    sources: dict = {}
    apps = _od39_app_ids()
    if apps:
        sources[OD39_SOURCE_ID] = set(apps)
    if owner_id and installed(conn):
        for source_id, app_id in conn.execute(
                f"SELECT source_id, app_id FROM {RECEIPTS} WHERE owner_id=? AND revoked_at IS NULL", (owner_id,)):
            sources.setdefault(source_id, set()).add(app_id)
    return {source: frozenset(ids) for source, ids in sources.items()}


def attested_revisions(conn, *, owner_id: str, source_id: str, message_id: str, conversation_id: str) -> frozenset:
    """Content revisions of this row that a live receipt of this owner, for this source, lists."""
    if not installed(conn):
        return frozenset()
    return frozenset(r[0] for r in conn.execute(
        f"SELECT r.content_revision FROM {RECEIPT_ROWS} r JOIN {RECEIPTS} t ON t.receipt_id=r.receipt_id "
        "WHERE r.message_id=? AND r.conversation_id=? AND t.owner_id=? AND t.source_id=? AND t.revoked_at IS NULL",
        (message_id, conversation_id, owner_id, source_id)))


def attested_datasets(conn, *, owner_id: str, source_id: str, message_id: str, conversation_id: str,
                      content_revision: str) -> frozenset:
    """Datasets (None for a receipt that certified none) of the live receipts listing this row at this revision."""
    if not installed(conn) or "dataset_id" not in _columns(conn, RECEIPTS):
        return frozenset()
    return frozenset(r[0] for r in conn.execute(
        f"SELECT t.dataset_id FROM {RECEIPT_ROWS} r JOIN {RECEIPTS} t ON t.receipt_id=r.receipt_id "
        "WHERE r.message_id=? AND r.conversation_id=? AND r.content_revision=? AND t.owner_id=? AND t.source_id=? "
        "AND t.revoked_at IS NULL", (message_id, conversation_id, content_revision, owner_id, source_id)))


def install_dataset(conn, *, owner_id: str, source_id: str) -> Optional[str]:
    """The dataset this owner's pre-stamp rows of this source came in through, or None when that is not provable.

    Nothing on a pre-stamp row says which dataset its write went to. The node's
    install record can, by elimination: when this source has exactly one active
    install and every install it ever recorded that could serve this owner is
    scoped to the same concrete dataset, there was no other dataset for the
    capture to write through. An unscoped or unreadable install, a wildcard
    dataset, or two datasets leave it unprovable.
    """
    owner, source = _text(owner_id), _text(source_id)
    if owner is None or source is None:
        return None
    found = conn.execute("SELECT type FROM sqlite_master WHERE name='source_runtime_installs'").fetchmany(2)
    if (len(found) != 1 or found[0][0] != "table"
            or not {"source_id", "scope_key", "is_active", "status"} <= _columns(conn, "source_runtime_installs")):
        return None
    datasets, active = set(), []
    for scope_key, is_active, status in conn.execute(
            "SELECT scope_key, is_active, status FROM source_runtime_installs WHERE source_id=?", (source,)):
        try:
            scope = json.loads(scope_key) if isinstance(scope_key, str) else None
        except ValueError:
            scope = None
        if not isinstance(scope, dict):
            return None
        if scope.get("user_id") not in (owner, "*"):
            continue
        if is_active != 0:
            active.append((is_active, status))
        datasets.add(scope.get("dataset_id"))
    if len(active) != 1 or type(active[0][0]) is not int or active[0][0] != 1 or active[0][1] not in INSTALL_LIVE:
        return None
    dataset = next(iter(datasets)) if len(datasets) == 1 else None
    return dataset if _text(dataset) and dataset != "*" else None


def certified_dataset(conn, *, owner_id: str, row: dict) -> Optional[str]:
    """RD5: the dataset this AI-chat row was written through, from the node's own record, or None.

    A door-written row names it in ``writer_dataset_id`` (with its writer class,
    never from the payload). A pre-stamp row has only this owner's live receipts
    at the row's current revision, and they must agree on one dataset.
    """
    from ..features.provenance.writer_class import normalize_writer_class

    if _text(owner_id) is None or _text(row.get("source_id")) is None:
        return None
    if normalize_writer_class(row.get("writer_class")) is not None:
        return _text(row.get("writer_dataset_id"))
    found = attested_datasets(conn, owner_id=owner_id, source_id=row.get("source_id"),
        message_id=row.get("message_id"), conversation_id=row.get("conversation_id"),
        content_revision=content_revision(row))
    from .capture_receipts import ai_chat_export_source, attested_datasets as import_datasets
    if ai_chat_export_source(row.get("source_id")):
        # The owner's receipt over an export import (see the module docstring); both kinds must agree on one.
        found = found | import_datasets(conn, owner_id=owner_id, table="ai_chat_messages", row=row)
    return _text(next(iter(found))) if len(found) == 1 else None


def _parent_bound(conn, *, owner_id: str, source_id: str, conversation_id: Any) -> bool:
    parents = conn.execute("SELECT owner_user_id, source_id FROM ai_chat_conversations WHERE conversation_id=?",
                           (conversation_id,)).fetchmany(2)
    return len(parents) == 1 and tuple(parents[0]) == (owner_id, source_id)


def capture_proven(conn, *, owner_id: str, identity_source_id: str, row: dict) -> bool:
    """True only for a user-role row of this owner's own capture (see the module docstring)."""
    from ..features.provenance.writer_class import WRITER_OWNER_APP, WRITER_OWNER_IMPORT, normalize_writer_class
    from .ingest_protocol import CHATGPT_SOURCE_ID

    source_id = row.get("source_id")
    if (not _text(owner_id) or row.get("sender_type") not in USER_ROLES or not _text(source_id)
            or source_id != identity_source_id or source_id == CHATGPT_SOURCE_ID):
        return False
    apps = capture_sources(conn, owner_id).get(source_id)
    if not _parent_bound(conn, owner_id=owner_id, source_id=source_id, conversation_id=row.get("conversation_id")):
        return False
    if not apps:
        return _import_proven(conn, owner_id=owner_id, row=row)
    writer = normalize_writer_class(row.get("writer_class"))
    if writer == WRITER_OWNER_APP:
        return row.get("writer_app_id") in apps
    if writer == WRITER_OWNER_IMPORT:
        return True
    if writer is not None:
        return False
    return content_revision(row) in attested_revisions(conn, owner_id=owner_id, source_id=source_id,
        message_id=row.get("message_id"), conversation_id=row.get("conversation_id"))


def _import_proven(conn, *, owner_id: str, row: dict) -> bool:
    """A user-role row of an export import source, proven by its door or by the owner's receipt over the import.

    The caller has checked the role, the identity's source and the parent. An export is imported, so only the
    owner's import door (``owner_import``) or, for a pre-stamp row, a live receipt counts; an app's stamp never
    does (``capture_receipts.proven`` would accept an attested app's).
    """
    from ..features.provenance.writer_class import WRITER_OWNER_IMPORT, normalize_writer_class
    from .capture_receipts import ai_chat_export_source, proven

    if not ai_chat_export_source(row.get("source_id")):
        return False
    if normalize_writer_class(row.get("writer_class")) not in (None, WRITER_OWNER_IMPORT):
        return False
    return proven(conn, owner_id=owner_id, table="ai_chat_messages", identity_source_id=row.get("source_id"),
                  row=row)


# --- the owner's one-time attestation of pre-stamp rows ------------------------------------

def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def eligible_rows(conn, *, owner_id: str, source_id: str) -> list:
    """(message_id, conversation_id, content_revision) of every pre-stamp user row an attestation would cover.

    A pre-stamp row has no writer class recorded. Its parent must be this owner's
    conversation of this source, as :func:`capture_proven` requires. Rows a live
    receipt already lists at their current revision are left out.
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('ai_chat_messages','ai_chat_conversations')")}
    if tables != {"ai_chat_messages", "ai_chat_conversations"}:
        return []
    writer = "m.writer_class IS NULL" if "writer_class" in _columns(conn, "ai_chat_messages") else "1"
    rows = conn.execute(
        "SELECT m.message_id, m.conversation_id, m.source_id, m.sender_type, m.content FROM ai_chat_messages m "
        "JOIN ai_chat_conversations c ON c.conversation_id=m.conversation_id "
        f"WHERE m.source_id=? AND m.sender_type IN ('human','user') AND {writer} "
        "AND c.owner_user_id=? AND c.source_id=? ORDER BY m.message_id",
        (source_id, owner_id, source_id)).fetchall()
    found = []
    for message_id, conversation_id, row_source, sender_type, content in rows:
        if not _parent_bound(conn, owner_id=owner_id, source_id=source_id, conversation_id=conversation_id):
            continue
        revision = content_revision({"message_id": message_id, "conversation_id": conversation_id,
                                     "source_id": row_source, "sender_type": sender_type, "content": content})
        if revision in attested_revisions(conn, owner_id=owner_id, source_id=source_id, message_id=message_id,
                                          conversation_id=conversation_id):
            continue
        found.append((message_id, conversation_id, revision))
    return found


def _check_request(owner_id: Any, source_id: Any, app_id: Any) -> tuple:
    from .ingest_protocol import CHATGPT_SOURCE_ID
    owner, source, app = _text(owner_id), _text(source_id), _text(app_id)
    if owner is None:
        raise PolicyError("owner_authority_required")
    # The export lane keeps its own proof; a receipt never stands in for it.
    if source is None or app is None or source == CHATGPT_SOURCE_ID:
        raise PolicyError("capture_attestation_invalid")
    return owner, source, app


def _summary(owner: str, source: str, app: str, rows: list, dataset: Optional[str]) -> dict:
    return {"version": VERSION, "source_id": source, "app_id": app, "row_count": len(rows),
            "conversation_count": len({r[1] for r in rows}), "statement": STATEMENT,
            "dataset_certified": dataset is not None,
            "preview_digest": digest({"version": VERSION, "owner_id": owner, "source_id": source, "app_id": app,
                                      "dataset_id": dataset, "rows": [list(r) for r in rows]})}


def preview(conn, *, owner_id: str, source_id: str, app_id: str) -> dict:
    """Counts, whether a dataset is certified, and the digest the owner confirms. No ids, no content."""
    owner, source, app = _check_request(owner_id, source_id, app_id)
    return _summary(owner, source, app, eligible_rows(conn, owner_id=owner, source_id=source),
                    install_dataset(conn, owner_id=owner, source_id=source))


def attest(conn, *, owner_id: str, source_id: str, app_id: str, preview_digest: Any, confirm: Any,
           now: Optional[int] = None) -> dict:
    """Record the owner's receipt for exactly the rows the confirmed preview named. The caller commits."""
    owner, source, app = _check_request(owner_id, source_id, app_id)
    if confirm is not True:
        raise PolicyError("capture_attestation_unconfirmed")
    rows = eligible_rows(conn, owner_id=owner, source_id=source)
    dataset = install_dataset(conn, owner_id=owner, source_id=source)
    summary = _summary(owner, source, app, rows, dataset)
    if preview_digest != summary["preview_digest"]:
        raise PolicyError("capture_attestation_preview_stale")
    install(conn)
    receipt_id = "aicap-" + uuid.uuid4().hex
    attested_at = int(time.time() if now is None else now)
    conn.execute(f"INSERT INTO {RECEIPTS} (receipt_id, version, owner_id, source_id, app_id, statement, preview_digest, "
                 "row_count, attested_at, revoked_at, dataset_id) VALUES (?,?,?,?,?,?,?,?,?,NULL,?)",
                 (receipt_id, VERSION, owner, source, app, STATEMENT, summary["preview_digest"], len(rows), attested_at,
                  dataset))
    conn.executemany(f"INSERT INTO {RECEIPT_ROWS} (receipt_id, message_id, conversation_id, content_revision) "
                     "VALUES (?,?,?,?)", [(receipt_id, *row) for row in rows])
    return {"receipt_id": receipt_id, "version": VERSION, "source_id": source, "app_id": app,
            "row_count": len(rows), "dataset_certified": dataset is not None,
            "preview_digest": summary["preview_digest"], "attested_at": attested_at}


def revoke(conn, *, owner_id: str, receipt_id: Any, now: Optional[int] = None) -> dict:
    """Withdraw a receipt: its rows and its (source, app) stop counting. The caller commits."""
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
    fields = ("receipt_id", "version", "source_id", "app_id", "row_count", "preview_digest", "attested_at", "revoked_at")
    dataset = "dataset_id IS NOT NULL" if "dataset_id" in _columns(conn, RECEIPTS) else "0"
    return [{**dict(zip(fields, row[:-1])), "dataset_certified": bool(row[-1])} for row in conn.execute(
        f"SELECT {', '.join(fields)}, {dataset} FROM {RECEIPTS} WHERE owner_id=? ORDER BY attested_at DESC, receipt_id",
        (owner_id,))]

