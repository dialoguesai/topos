"""Local-first identity: a node that runs before any account exists.

A node needs an owner id and a Topos id before it can store a row — every
canonical dataset is named ``<user_id>:topos:<topos_id>`` and the row-level
owner stamps (``owner_user_id``) read the same id. Until now both came from the
control plane at the first ``connection_info``, so a node with no account could
not ingest anything and ``resolve_owner_scope`` answered ``not_paired``.

Local-only mode mints both on this computer: an owner id under the
``local:`` prefix and a Topos id shaped like the control plane's. The node runs,
collects and answers with them. When the person connects an account later, the
first ``connection_info`` that carries the real ids runs :func:`rescope_to_account`
ONCE: every table that stamps the owner or names a dataset is rewritten in one
transaction, the raw-file folders are renamed, and the engine config switches to
``identity_mode = account``. After that the ordinary "never rewrite user_id"
rule (``core/handlers/device.py``, ``permissions_v2/self_bind.py``) applies again.

Nothing here reads or writes a key.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LOCAL_OWNER_PREFIX = "local:"
IDENTITY_MODE_KEY = "identity_mode"
IDENTITY_MODE_LOCAL = "local"
IDENTITY_MODE_ACCOUNT = "account"
RESCOPE_REPORT_KEY = "rescope_report"
PREVIOUS_LOCAL_IDENTITY_KEY = "previous_local_identity"

#: Written last, by key, never swept like ordinary rows.
_ENGINE_CONFIG_TABLE = "engine_config"


class RescopeRefused(RuntimeError):
    """The database is not in local mode, or holds no identity to move."""


def is_local_owner_id(user_id: Optional[str]) -> bool:
    return str(user_id or "").startswith(LOCAL_OWNER_PREFIX)


def mint_local_owner_id() -> str:
    return LOCAL_OWNER_PREFIX + uuid.uuid4().hex


def mint_local_topos_id() -> str:
    # The control plane's ids are UUIDs; the same shape lets the "attach" pairing
    # adopt this one instead of issuing another.
    return str(uuid.uuid4())


def identity_mode(conn: sqlite3.Connection) -> str:
    """``local`` | ``account`` | ``none`` for the served database."""
    from .core.state import get_engine_config_value, get_user_id

    stored = str(get_engine_config_value(conn, IDENTITY_MODE_KEY) or "").strip()
    if stored in (IDENTITY_MODE_LOCAL, IDENTITY_MODE_ACCOUNT):
        return stored
    user_id = str(get_user_id(conn) or "").strip()
    if not user_id:
        return "none"
    return IDENTITY_MODE_LOCAL if is_local_owner_id(user_id) else IDENTITY_MODE_ACCOUNT


def ensure_local_identity(conn: sqlite3.Connection) -> Optional[Dict[str, str]]:
    """Mint the local owner and Topos ids when the database holds neither.

    Returns the ids it wrote, or None when the database already has an identity
    (local or account) — then nothing is touched. A database with only one of
    the two ids (a half-written pairing) is left alone too: that is a pairing
    state, not a fresh node.
    """
    from .core.state import get_engine_config_value, get_user_id, set_engine_config_value, store_user_id

    user_id = str(get_user_id(conn) or "").strip()
    topos_id = str(get_engine_config_value(conn, "topos_id") or "").strip()
    if user_id or topos_id:
        return None
    minted = {"user_id": mint_local_owner_id(), "topos_id": mint_local_topos_id()}
    store_user_id(conn, minted["user_id"])
    set_engine_config_value(conn, "topos_id", minted["topos_id"])
    set_engine_config_value(conn, IDENTITY_MODE_KEY, IDENTITY_MODE_LOCAL)
    logger.info("Local-only identity minted: this Topos runs without an account until one is connected.")
    return minted


def _tables(conn: sqlite3.Connection) -> List[str]:
    """Ordinary tables only. Virtual tables (the embedding index, FTS) are keyed by rowid, not by owner, and
    ``PRAGMA table_info`` on one needs its extension loaded; their shadow tables are plain and carry no
    owner column."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND (sql IS NULL OR sql NOT LIKE 'CREATE VIRTUAL TABLE%') ORDER BY name"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _safe_dataset_dir(dataset_id: str) -> str:
    # Mirrors storage/raw/file_store.py get_file_path.
    return dataset_id.replace(":", "_").replace("/", "_")


def _rename_raw_dirs(base: Path, old_user: str, new_user: str, old_topos: str, new_topos: str) -> List[str]:
    """Rename raw-file folders named after the old datasets. Best effort; a miss is reported, never fatal."""
    renamed: List[str] = []
    if not base.is_dir():
        return renamed
    old_prefix = _safe_dataset_dir(old_user + ":")
    for child in sorted(base.iterdir()):
        if not child.is_dir() or not child.name.startswith(old_prefix):
            continue
        rest = child.name[len(old_prefix):]
        if old_topos != new_topos:
            rest = rest.replace(_safe_dataset_dir(f"topos:{old_topos}"), _safe_dataset_dir(f"topos:{new_topos}"), 1)
        target = base / (_safe_dataset_dir(new_user + ":") + rest)
        if target.exists():
            logger.warning("rescope: raw folder %s already exists; leaving %s in place", target.name, child.name)
            continue
        try:
            os.rename(child, target)
            renamed.append(child.name)
        except OSError as exc:
            logger.warning("rescope: could not rename raw folder %s: %s", child.name, type(exc).__name__)
    return renamed


def rescope_to_account(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    topos_id: Optional[str] = None,
    raw_base_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Move every row written under the local identity to the account's, once.

    ``user_id`` is the account's owner id (from the control plane's
    ``connection_info``). ``topos_id`` is the id the control plane assigned; when
    it is None or equal to the local one the Topos id is kept (the control plane
    adopted the node's id).

    One transaction. Every text column of every ordinary table is rewritten
    where it contains the old owner id or (when it changes) the old Topos id:
    the canonical dataset id ``<old_user>:topos:<old_topos>`` becomes the
    account's, any other ``<old_user>:…`` dataset keeps its tail, and bare ids
    are replaced. That reaches the stamped columns (``owner_user_id``,
    ``dataset_id``, ``writer_dataset_id``, ``scope_key``) and the ids embedded
    in JSON payloads (``payload_json``, ``detail_json``) alike — a 5 Nov 2026
    scan of a fresh node found the ids in both kinds. The local owner id is a
    ``local:`` + 32-hex string, so a substring match cannot hit anything else.
    ``engine_config`` is written last so the ingest provenance clock
    (``permissions_v2/ingest_provenance.py``) advances on the real owner change
    exactly as it would for any other. Raw-file folders are renamed after the
    commit. The report (row updates per table and column) is stored under
    ``engine_config.rescope_report`` and returned.
    """
    from .core.state import get_engine_config_value, get_user_id, with_db_write

    new_user = str(user_id or "").strip()
    if not new_user:
        raise RescopeRefused("no account user id")
    if is_local_owner_id(new_user):
        raise RescopeRefused("the account id is a local id")
    old_user = str(get_user_id(conn) or "").strip()
    old_topos = str(get_engine_config_value(conn, "topos_id") or "").strip()
    if identity_mode(conn) != IDENTITY_MODE_LOCAL or not old_user or not old_topos:
        raise RescopeRefused("this Topos is not in local mode")
    new_topos = str(topos_id or "").strip() or old_topos

    report: Dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "topos_id_changed": new_topos != old_topos,
        "tables": {},
        "raw_folders_renamed": [],
    }
    old_ds, new_ds = f"{old_user}:topos:{old_topos}", f"{new_user}:topos:{new_topos}"
    topos_changes = new_topos != old_topos

    with with_db_write():
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            for table in _tables(conn):
                if table == _ENGINE_CONFIG_TABLE:
                    continue
                touched = 0
                for col in _columns(conn, table):
                    qcol = _q(col)
                    # The full dataset id first (it carries the topos id), then the bare owner id,
                    # then the bare topos id when the control plane assigned a new one.
                    expr = f"replace(replace({qcol}, ?, ?), ?, ?)"
                    args: List[Any] = [old_ds, new_ds, old_user, new_user]
                    where = f"typeof({qcol})='text' AND (instr({qcol}, ?) > 0"
                    where_args: List[Any] = [old_user]
                    if topos_changes:
                        expr = f"replace({expr}, ?, ?)"
                        args += [old_topos, new_topos]
                        where += f" OR instr({qcol}, ?) > 0"
                        where_args.append(old_topos)
                    where += ")"
                    touched += conn.execute(
                        f"UPDATE {_q(table)} SET {qcol}={expr} WHERE {where}", args + where_args
                    ).rowcount
                if touched:
                    report["tables"][table] = touched
            # engine_config last: the BEFORE trigger on user_id bumps the provenance generation once.
            conn.execute(
                f"INSERT OR REPLACE INTO {_ENGINE_CONFIG_TABLE} (key, value) VALUES (?, ?)",
                (PREVIOUS_LOCAL_IDENTITY_KEY, json.dumps({"user_id": old_user, "topos_id": old_topos, "at": report["at"]})),
            )
            conn.execute(f"INSERT OR REPLACE INTO {_ENGINE_CONFIG_TABLE} (key, value) VALUES ('user_id', ?)", (new_user,))
            conn.execute(f"INSERT OR REPLACE INTO {_ENGINE_CONFIG_TABLE} (key, value) VALUES ('topos_id', ?)", (new_topos,))
            conn.execute(
                f"INSERT OR REPLACE INTO {_ENGINE_CONFIG_TABLE} (key, value) VALUES (?, ?)",
                (IDENTITY_MODE_KEY, IDENTITY_MODE_ACCOUNT),
            )
            conn.execute(
                f"INSERT OR REPLACE INTO {_ENGINE_CONFIG_TABLE} (key, value) VALUES (?, ?)",
                (RESCOPE_REPORT_KEY, json.dumps(report)),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    if raw_base_path is None:
        try:
            from .storage.raw.file_store import active_ingestion_base

            raw_base_path = Path(active_ingestion_base())
        except Exception:  # noqa: BLE001 — no raw store configured, nothing to rename
            raw_base_path = None
    if raw_base_path is not None:
        report["raw_folders_renamed"] = _rename_raw_dirs(raw_base_path, old_user, new_user, old_topos, new_topos)
        try:
            from .core.state import set_engine_config_value

            set_engine_config_value(conn, RESCOPE_REPORT_KEY, json.dumps(report))
        except Exception:  # noqa: BLE001 — the report is a convenience
            pass
    logger.info(
        "Local identity moved to the account: %d tables rewritten, topos id %s.",
        len(report["tables"]), "changed" if report["topos_id_changed"] else "kept",
    )
    return report
